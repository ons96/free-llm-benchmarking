#!/usr/bin/env python3
"""NVIDIA NIM TTFT/TPS benchmark with real streaming measurements."""

import asyncio
import json
import os
import sqlite3
import sys
import time

import httpx

API_KEY = os.environ.get("NVIDIA_API_KEY", "")
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_TOKENS = 200
TIMEOUT = 30
CONCURRENCY = 4
RETRIES = 2

DB_PATH = Path(__file__).parent / "data" / "nvidia_speedtest.db"

PROMPT = {
    "role": "user",
    "content": "Write a short 4-line Python function that returns the Fibonacci sequence up to n. Use type hints and a docstring."
}


def get_all_models():
    print("Fetching model list from NVIDIA...")
    resp = httpx.get(
        f"{BASE_URL}/models",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    models = [m["id"] for m in data.get("data", [])]
    print(f"  Found {len(models)} models")
    return models


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            ttft_ms REAL,
            tps REAL,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            total_time_ms REAL,
            status TEXT NOT NULL,
            error_message TEXT,
            UNIQUE(model)
        )
    """)
    conn.commit()
    return conn


async def test_model_streaming(client, model):
    url = f"{BASE_URL}/chat/completions"
    body = {
        "model": model,
        "messages": [PROMPT],
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.0,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "text/event-stream",
    }

    for attempt in range(1 + RETRIES):
        t_start = time.monotonic()
        ttft_ms = None
        first_content_at = None
        token_count = 0
        output_text = []
        prompt_tokens = 0
        total_tokens = 0
        try:
            async with client.stream("POST", url, json=body, headers=headers, timeout=TIMEOUT) as resp:
                if resp.status_code != 200:
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    if resp.status_code in (429, 503):
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    t_end = time.monotonic()
                    return {
                        "model": model, "ttft_ms": None, "tps": None,
                        "prompt_tokens": 0, "output_tokens": 0,
                        "total_time_ms": (t_end - t_start) * 1000,
                        "status": "error",
                        "error_message": f"HTTP {resp.status_code}: {body_text}",
                    }

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            now = time.monotonic()
                            if first_content_at is None:
                                first_content_at = now
                            output_text.append(content)
                            token_count += 1
                    usage = chunk.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        total_tokens = usage.get("total_tokens", 0)

            t_end = time.monotonic()
            total_ms = (t_end - t_start) * 1000
            if first_content_at is not None:
                ttft_ms = (first_content_at - t_start) * 1000
            gen_ms = total_ms - (ttft_ms or 0)
            tps = (token_count / (gen_ms / 1000)) if gen_ms > 0 and token_count > 0 else 0
            return {
                "model": model, "ttft_ms": ttft_ms, "tps": tps,
                "prompt_tokens": prompt_tokens, "output_tokens": token_count,
                "total_time_ms": total_ms, "status": "success",
                "error_message": None,
            }

        except httpx.TimeoutException:
            t_end = time.monotonic()
            return {
                "model": model, "ttft_ms": None, "tps": None,
                "prompt_tokens": 0, "output_tokens": token_count,
                "total_time_ms": (t_end - t_start) * 1000,
                "status": "timeout", "error_message": "Timeout",
            }
        except Exception as e:
            t_end = time.monotonic()
            return {
                "model": model, "ttft_ms": None, "tps": None,
                "prompt_tokens": 0, "output_tokens": 0,
                "total_time_ms": (t_end - t_start) * 1000,
                "status": "error", "error_message": f"{type(e).__name__}: {str(e)[:200]}",
            }

    return {
        "model": model, "ttft_ms": None, "tps": None,
        "prompt_tokens": 0, "output_tokens": 0,
        "total_time_ms": None, "status": "rate_limited",
        "error_message": "All retries exhausted",
    }


def save_result(conn, result):
    conn.execute("""
        INSERT OR REPLACE INTO results
        (model, timestamp, ttft_ms, tps, prompt_tokens, output_tokens, total_time_ms, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["model"],
        datetime.utcnow().isoformat() + "Z",
        result["ttft_ms"],
        result["tps"],
        result["prompt_tokens"],
        result["output_tokens"],
        result["total_time_ms"],
        result["status"],
        result["error_message"],
    ))
    conn.commit()


async def run_tests(models, conn, force=False):
    if force:
        to_test = models
        conn.execute("DELETE FROM results")
        conn.commit()
    else:
        existing = set(r[0] for r in conn.execute("SELECT model FROM results WHERE status = 'success'").fetchall())
        to_test = [m for m in models if m not in existing]

    print(f"\nAlready tested (success): {len(models) - len(to_test)}")
    print(f"To test: {len(to_test)}")

    if not to_test:
        print("All models already tested!")
        return

    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    total = len(to_test)
    results = []

    async def test_with_sem(model):
        nonlocal completed
        async with semaphore:
            async with httpx.AsyncClient() as client:
                result = await test_model_streaming(client, model)
            completed += 1
            save_result(conn, result)
            results.append(result)

            status_icon = "OK" if result["status"] == "success" else "FAIL"
            ttft = f"{result['ttft_ms']:.0f}ms" if result["ttft_ms"] is not None else "-"
            tps = f"{result['tps']:.1f}" if result["tps"] else "-"
            tot = f"{result['total_time_ms']:.0f}ms" if result["total_time_ms"] else "-"
            err = result["error_message"][:50] if result["error_message"] else ""
            print(f"[{completed:>3}/{total}] {status_icon:4s} {model[:55]:55s} ttft={ttft:>7} tps={tps:>6} total={tot:>7} {err}", flush=True)
            await asyncio.sleep(0.3)

    tasks = [asyncio.create_task(test_with_sem(m)) for m in to_test]
    await asyncio.gather(*tasks)
    return results


def print_summary(conn):
    print("\n" + "=" * 100)
    print("SUMMARY (sorted by TPS, success only)")
    print("=" * 100)
    rows = conn.execute("""
        SELECT model, ttft_ms, tps, output_tokens, total_time_ms, status
        FROM results
        WHERE status = 'success' AND tps > 0
        ORDER BY tps DESC
    """).fetchall()
    print(f"{'Model':<55} {'TTFT':>9} {'TPS':>7} {'Out':>5} {'Total':>9}")
    print("-" * 100)
    for model, ttft, tps, out, total, _ in rows[:50]:
        print(f"{model[:55]:<55} {ttft:>7.0f}ms {tps:>6.1f} {out:>5} {total:>7.0f}ms")

    print(f"\n  {len(rows)} successful models")

    err_rows = conn.execute("""
        SELECT status, COUNT(*) FROM results GROUP BY status
    """).fetchall()
    print(f"  Status breakdown: {dict(err_rows)}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    only_arg = [a for a in sys.argv if a.startswith("--only=")]
    only = only_arg[0].split("=", 1)[1].split(",") if only_arg else None

    conn = init_db()
    models = get_all_models()
    if only:
        models = [m for m in models if m in only]
        print(f"Filtered to {len(models)} models: {models}")

    asyncio.run(run_tests(models, conn, force=force))
    print_summary(conn)
