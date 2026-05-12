#!/usr/bin/env python3
"""Generate top-10 summary markdown for GitHub Actions summary."""
import json
import sys
import csv
from pathlib import Path

def main():
    csv_path = Path("data/leaderboard.csv")
    if not csv_path.exists():
        print("No benchmark data available (leaderboard.csv not found).")
        return

    try:
        rows = list(csv.DictReader(csv_path.open()))
    except Exception as e:
        print(f"Error reading leaderboard: {e}", file=sys.stderr)
        return

    if not rows:
        print("No benchmark data available.")
        return

    print(f"Found {len(rows)} models. Showing top 10:")
    for i, r in enumerate(rows[:10], 1):
        provider = r.get("provider", "?")
        model = r.get("model", "?")
        ttft = r.get("TTFT_sec", "?")
        tps = r.get("TPS", "?")
        rank = r.get("rank", i)
        print(f"#{rank} {provider}/{model}: TTFT={ttft}s TPS={tps}")

if __name__ == "__main__":
    main()
