#!/usr/bin/env python3
"""
Parallel benchmark runner - tests multiple providers simultaneously.
Usage: python3 scripts/parallel_benchmark.py --providers opencode,nvidia,blazeai --concurrency 10
"""
import asyncio
import argparse
import subprocess
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())


def run_benchmark(provider: str, concurrency: int, runs: int, timeout_min: int) -> dict:
    """Run a single provider benchmark and return results."""
    start = time.time()
    cmd = [
        sys.executable, "cli.py", "test",
        "--provider", provider,
        "--concurrency", str(concurrency),
        "--yes",
        "--runs", str(runs),
    ]
    print(f"[{provider}] Starting...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_min * 60,
            cwd=os.getcwd(),
        )
        elapsed = time.time() - start
        success = result.returncode == 0
        # Count successes from DB
        import sqlite3
        db = sqlite3.connect("data/speedrun.db")
        cur = db.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM speed_tests WHERE provider_name=? AND status='success'",
            (provider,)
        )
        count = cur.fetchone()[0]
        db.close()
        print(f"[{provider}] Done in {elapsed:.0f}s - {count} successes")
        return {"provider": provider, "success": success, "elapsed": elapsed, "count": count}
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        print(f"[{provider}] TIMEOUT after {elapsed:.0f}s")
        return {"provider": provider, "success": False, "elapsed": elapsed, "count": 0, "error": "timeout"}
    except Exception as e:
        elapsed = time.time() - start
        print(f"[{provider}] ERROR: {e}")
        return {"provider": provider, "success": False, "elapsed": elapsed, "count": 0, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Parallel benchmark runner")
    parser.add_argument("--providers", default="opencode,nvidia,blazeai,logfare,wiwi,ktai,ollama-cloud,huggingface,modelscope,cerebras,together,openrouter,openai",
                        help="Comma-separated provider names")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrency per provider")
    parser.add_argument("--runs", type=int, default=1, help="Runs per model")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout per provider (minutes)")
    parser.add_argument("--max-parallel", type=int, default=3, help="Max parallel providers")
    args = parser.parse_args()

    providers = [p.strip() for p in args.providers.split(",")]
    print(f"Testing {len(providers)} providers: {providers}")
    print(f"Concurrency={args.concurrency}, Runs={args.runs}, Timeout={args.timeout}min, Parallel={args.max_parallel}")

    results = []
    with ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
        futures = {
            executor.submit(run_benchmark, p, args.concurrency, args.runs, args.timeout): p
            for p in providers
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    print("\n=== SUMMARY ===")
    for r in sorted(results, key=lambda x: -x.get("count", 0)):
        status = "OK" if r["success"] else "FAIL"
        print(f"  {r['provider']}: {status}, {r.get('count', 0)} success, {r['elapsed']:.0f}s")
    total_success = sum(r.get("count", 0) for r in results)
    print(f"\nTotal successes: {total_success}")


if __name__ == "__main__":
    main()
