#!/usr/bin/env python3
"""NVIDIA NIM TTFT/TPS benchmark with real streaming measurements.

Refreshed (2026-08) to use the shared measurement core in
``benchmarks/measurement.py`` so TTFT/TPS/token counting are identical
to the main runner:

- streaming-aware TTFT (first *content* chunk, not connection time)
- token counting from the provider ``usage`` block with a chars/4
  fallback (``token_source`` records which one was used)
- burst/fake-streaming detection so buffered proxies can't fabricate
  huge TPS numbers
- per-provider result records with UTC timestamps written to SQLite,
  JSON and CSV

Usage:
    NVIDIA_API_KEY=... python nvidia_speedtest_v2.py [--force] [--only=m1,m2]
    BASE_URL=... python nvidia_speedtest_v2.py   # any OpenAI-compat endpoint

Outputs (under data/):
    nvidia_speedtest.db            SQLite (results table, one row per model)
    nvidia_speedtest_results.json  full per-call records, timestamps
    nvidia_speedtest_results.csv   same records in CSV form
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from benchmarks.measurement import (
    BenchmarkRecord,
    load_json,
    measure_openai_stream,
    write_csv,
    write_json,
)

API_KEY = os.environ.get("NVIDIA_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "https://integrate.api.nvidia.com/v1")
MAX_TOKENS = 200
TIMEOUT = 30
CONCURRENCY = 4
RETRIES = 2

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "data" / "nvidia_speedtest.db"
JSON_PATH = REPO_ROOT / "data" / "nvidia_speedtest_results.json"
CSV_PATH = REPO_ROOT / "data" / "nvidia_speedtest_results.csv"

PROMPT = {
    "role": "user",
    "content": "Write a short 4-line Python function that returns the Fibonacci sequence up to n. Use type hints and a docstring."
}


def get_all_models():
    print(f"Fetching model list from {BASE_URL} ...")
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
            token_source TEXT,
            UNIQUE(model)
        )
    """)
    conn.commit()
    return conn


async def test_model_streaming(client, model):
    """One streaming measurement; returns a BenchmarkRecord."""
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
        result = await measure_openai_stream(client, url, body, headers, timeout=TIMEOUT)
        if result["status"] == "http_error" and result["error_message"].startswith(("HTTP 429", "HTTP 503")):
            await asyncio.sleep(2 + attempt * 2)
            continue
        break

    return BenchmarkRecord(
        provider="nvidia",
        model=model,
        base_url=BASE_URL,
        status=result["status"],
        ttft_sec=result.get("ttft_sec"),
        tps=result.get("tps"),
        output_tokens=result.get("output_tokens", 0),
        prompt_tokens=result.get("prompt_tokens"),
        total_time_sec=result.get("total_time_sec"),
        token_source=result.get("token_source", "estimated"),
        streaming=True,
        error_message=result.get("error_message"),
    )


def save_result(conn, record: BenchmarkRecord):
    conn.execute("""
        INSERT OR REPLACE INTO results
        (model, timestamp, ttft_ms, tps, prompt_tokens, output_tokens, total_time_ms, status, error_message, token_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.model,
        record.timestamp,
        record.ttft_sec * 1000 if record.ttft_sec is not None else None,
        record.tps,
        record.prompt_tokens,
        record.output_tokens,
        record.total_time_sec * 1000 if record.total_time_sec is not None else None,
        record.status,
        record.error_message,
        record.token_source,
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
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    total = len(to_test)
    records: list[BenchmarkRecord] = []

    async def test_with_sem(model):
        nonlocal completed
        async with semaphore:
            async with httpx.AsyncClient() as client:
                record = await test_model_streaming(client, model)
            completed += 1
            save_result(conn, record)
            records.append(record)

            status_icon = "OK" if record.status == "success" else "FAIL"
            ttft = f"{record.ttft_sec * 1000:.0f}ms" if record.ttft_sec is not None else "-"
            tps = f"{record.tps:.1f}" if record.tps else "-"
            tot = f"{record.total_time_sec * 1000:.0f}ms" if record.total_time_sec else "-"
            err = record.error_message[:50] if record.error_message else ""
            print(f"[{completed:>3}/{total}] {status_icon:4s} {model[:55]:55s} ttft={ttft:>7} tps={tps:>6} total={tot:>7} {err}", flush=True)
            await asyncio.sleep(0.3)

    tasks = [asyncio.create_task(test_with_sem(m)) for m in to_test]
    await asyncio.gather(*tasks)

    # JSON + CSV artifacts with timestamps (merge with any prior records)
    all_records = load_existing_records() + records
    write_json(all_records, JSON_PATH)
    write_csv(records, CSV_PATH, append=True)
    return records


def load_existing_records() -> list[BenchmarkRecord]:
    if not JSON_PATH.exists():
        return []
    try:
        return load_json(JSON_PATH)
    except (json.JSONDecodeError, OSError):
        return []


def print_summary(conn):
    print("\n" + "=" * 100)
    print("SUMMARY (sorted by TPS, success only)")
    print("=" * 100)
    rows = conn.execute("""
        SELECT model, ttft_ms, tps, output_tokens, total_time_ms, token_source, status
        FROM results
        WHERE status = 'success' AND tps > 0
        ORDER BY tps DESC
    """).fetchall()
    print(f"{'Model':<55} {'TTFT':>9} {'TPS':>7} {'Out':>5} {'Total':>9} {'Tokens':>8}")
    print("-" * 100)
    for model, ttft, tps, out, total, token_source, _ in rows[:50]:
        print(f"{model[:55]:<55} {ttft:>7.0f}ms {tps:>6.1f} {out:>5} {total:>7.0f}ms {token_source or '-':>8}")

    print(f"\n  {len(rows)} successful models")

    err_rows = conn.execute("""
        SELECT status, COUNT(*) FROM results GROUP BY status
    """).fetchall()
    print(f"  Status breakdown: {dict(err_rows)}")
    print(f"  Artifacts: {JSON_PATH.name}, {CSV_PATH.name}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    only_arg = [a for a in sys.argv if a.startswith("--only=")]
    only = only_arg[0].split("=", 1)[1].split(",") if only_arg else None

    conn = init_db()
    if only:
        models = only
        print(f"Filtered to {len(models)} models: {models}")
    else:
        models = get_all_models()

    asyncio.run(run_tests(models, conn, force=force))
    print_summary(conn)
