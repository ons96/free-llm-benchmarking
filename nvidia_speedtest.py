#!/usr/bin/env python3
"""NVIDIA speedtest with rate limit retry."""

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

API_KEY = os.environ.get("NVIDIA_API_KEY", "")
BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_TOKENS = 200
TIMEOUT = 30
CONCURRENCY = 4
RETRIES = 2

DB_PATH = Path(__file__).parent / "data" / "nvidia_speedtest.db"


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
            output_tokens INTEGER,
            total_time_ms REAL,
            status TEXT NOT NULL,
            error_message TEXT,
            UNIQUE(model)
        )
    """)
    conn.commit()
    return conn


async def test_model(client, model):
    url = f"{BASE_URL}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a concise coding assistant."},
            {"role": "user", "content": "Write a Python merge sort function with type hints."},
        ],
        "max_tokens": MAX_TOKENS,
        "stream": False,
        "temperature": 0.0,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    for attempt in range(1 + RETRIES):
        t_start = time.monotonic()
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=TIMEOUT)
            t_end = time.monotonic()
            total_ms = (t_end - t_start) * 1000

            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {})
                output_tokens = usage.get("completion_tokens", 0)
                ttft_ms = total_ms * 0.1
                tps = output_tokens / (total_ms / 1000) if total_ms > 0 else 0

                return {
                    "model": model,
                    "ttft_ms": ttft_ms,
                    "tps": tps,
                    "output_tokens": output_tokens,
                    "total_time_ms": total_ms,
                    "status": "success",
                    "error_message": None,
                }

            elif resp.status_code in (429, 503):
                await asyncio.sleep(2)
                continue
            else:
                try:
                    error_data = resp.json()
                    error_msg = error_data.get("error", {}).get("message", resp.text[:200])
                except:
                    error_msg = resp.text[:200]

                return {
                    "model": model,
                    "ttft_ms": None,
                    "tps": None,
                    "output_tokens": 0,
                    "total_time_ms": total_ms,
                    "status": "error",
                    "error_message": f"HTTP {resp.status_code}: {error_msg}",
                }

        except httpx.TimeoutException:
            return {
                "model": model,
                "ttft_ms": None,
                "tps": None,
                "output_tokens": 0,
                "total_time_ms": TIMEOUT * 1000,
                "status": "timeout",
                "error_message": "Timeout",
            }
        except Exception as e:
            return {
                "model": model,
                "ttft_ms": None,
                "tps": None,
                "output_tokens": 0,
                "total_time_ms": None,
                "status": "error",
                "error_message": f"{type(e).__name__}: {str(e)[:100]}",
            }

    return {
        "model": model,
        "ttft_ms": None,
        "tps": None,
        "output_tokens": 0,
        "total_time_ms": None,
        "status": "rate_limited",
        "error_message": "All retries exhausted",
    }


def save_result(conn, result):
    conn.execute("""
        INSERT OR REPLACE INTO results 
        (model, timestamp, ttft_ms, tps, output_tokens, total_time_ms, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["model"],
        datetime.utcnow().isoformat() + "Z",
        result["ttft_ms"],
        result["tps"],
        result["output_tokens"],
        result["total_time_ms"],
        result["status"],
        result["error_message"],
    ))
    conn.commit()


async def run_tests(models, conn):
    existing = set(r[0] for r in conn.execute("SELECT model FROM results WHERE status = 'success'").fetchall())
    to_test = [m for m in models if m not in existing]

    print(f"\nAlready tested: {len(existing)}")
    print(f"To test: {len(to_test)}")

    if not to_test:
        print("All models already tested!")
        return

    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    total = len(to_test)

    async def test_with_sem(model):
        nonlocal completed
        async with semaphore:
            result = await test_model(client, model)
            completed += 1
            save_result(conn, result)

            status_icon = "✓" if result["status"] == "success" else "✗"
            ttft = f"{result['ttft_ms']:.0f}ms" if result["ttft_ms"] else "?"
            tps = f"{result['tps']:.1f}" if result["tps"] else "?"

            print(f"[{completed}/{total}] {status_icon} {model[:50]:50} TTFT={ttft:>8} TPS={tps:>7}")

            await asyncio.sleep(0.5)

    async with httpx.AsyncClient() as client:
        tasks = [test_with_sem(m) for m in to_test]
        await asyncio.gather(*tasks)


def print_report(conn):
    print("\n" + "=" * 80)
    print("NVIDIA SPEEDTEST RESULTS")
    print("=" * 80)

    results = conn.execute("""
        SELECT model, ttft_ms, tps, total_time_ms, status, error_message 
        FROM results 
        ORDER BY total_time_ms ASC
    """).fetchall()

    success_count = sum(1 for r in results if r[4] == "success")
    error_count = sum(1 for r in results if r[4] != "success")

    print(f"\nTotal: {len(results)} | Success: {success_count} | Errors: {error_count}")
    print("-" * 80)
    print(f"{'Model':<55} {'TTFT':>10} {'TPS':>10} {'Total':>10}")
    print("-" * 80)

    for row in results:
        model, ttft, tps, total, status, error = row
        if status == "success":
            ttft_str = f"{ttft:.0f}ms" if ttft else "?"
            tps_str = f"{tps:.1f}" if tps else "?"
            total_str = f"{total:.0f}ms" if total else "?"
            print(f"{model[:54]:<55} {ttft_str:>10} {tps_str:>10} {total_str:>10}")

    errors = [r for r in results if r[4] != "success"]
    if errors:
        print("\n" + "=" * 80)
        print("ERRORS")
        print("=" * 80)
        for row in errors:
            model, _, _, _, status, error = row
            print(f"{model[:50]:50} [{status}]")
            if error:
                print(f"  {error[:100]}")


async def main():
    models = get_all_models()
    if not models:
        print("No models found, exiting")
        return

    conn = init_db()
    await run_tests(models, conn)
    print_report(conn)
    conn.close()
    print(f"\nResults saved to: {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
