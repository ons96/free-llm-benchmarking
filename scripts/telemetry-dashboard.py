#!/usr/bin/env python3
"""Telemetry dashboard: TPS/TTFT leaderboard from gateway snapshots.

Reads latest snapshot from ~/CodingProjects/llm-speedrun/data/gateway-telemetry-latest.db.
Output: CSV + ASCII table ranked by TPS desc.

Usage:
    python3 telemetry-dashboard.py
    python3 telemetry-dashboard.py --provider nvidia
    python3 telemetry-dashboard.py --model glm-5.1
    python3 telemetry-dashboard.py --since 2026-06-01
    python3 telemetry-dashboard.py --csv --out leaderboard.csv
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime
from statistics import median

DB_PATH = os.path.expanduser(
    os.environ.get("TELEMETRY_DB", "~/CodingProjects/llm-speedrun/data/gateway-telemetry-latest.db")
)


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = int(len(s) * p / 100)
    if k >= len(s):
        k = len(s) - 1
    return s[k]


def main():
    parser = argparse.ArgumentParser(description="Telemetry TPS/TTFT leaderboard")
    parser.add_argument("--db", default=DB_PATH, help="SQLite DB path")
    parser.add_argument("--provider", help="Filter by provider")
    parser.add_argument("--model", help="Filter by model")
    parser.add_argument("--since", help="Only events since YYYY-MM-DD")
    parser.add_argument("--csv", action="store_true", help="CSV output")
    parser.add_argument("--out", help="Output file (default stdout)")
    parser.add_argument("--limit", type=int, default=50, help="Max rows")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db, timeout=5)
    conn.row_factory = sqlite3.Row

    where = []
    params = []
    if args.provider:
        where.append("provider LIKE ?")
        params.append(f"%{args.provider}%")
    if args.model:
        where.append("model LIKE ?")
        params.append(f"%{args.model}%")
    if args.since:
        try:
            ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
            where.append("ts_start >= ?")
            params.append(ts)
        except ValueError:
            print(f"ERROR: bad --since format: {args.since} (use YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    query = f"""
        SELECT model, provider,
               COUNT(*) as total_requests,
               SUM(completion_tokens) as total_tokens,
               AVG(tps) as avg_tps,
               AVG(ttft_ms) as avg_ttft_ms,
               MIN(tps) as min_tps,
               MAX(tps) as max_tps
        FROM llm_events
        {where_clause}
        GROUP BY model, provider
        HAVING total_requests > 0
        ORDER BY avg_tps DESC
        LIMIT ?
    """
    params.append(args.limit)

    rows = conn.execute(query, params).fetchall()

    if not rows:
        print("No data found.", file=sys.stderr)
        sys.exit(0)

    # Per-row percentiles need separate queries (SQLite lacks PERCENTILE)
    output = []
    for row in rows:
        tps_values = [r[0] for r in conn.execute(
            "SELECT tps FROM llm_events WHERE model=? AND provider=? AND tps>0",
            (row["model"], row["provider"])
        ).fetchall()]
        output.append({
            "rank": 0,
            "model": row["model"],
            "provider": row["provider"],
            "avg_tps": round(row["avg_tps"] or 0, 2),
            "avg_ttft_ms": round(row["avg_ttft_ms"] or 0, 1),
            "p50_tps": round(median(tps_values) if tps_values else 0, 2),
            "p95_tps": round(percentile(tps_values, 95), 2),
            "total_requests": row["total_requests"],
            "total_tokens": row["total_tokens"] or 0,
        })

    for i, row in enumerate(output, 1):
        row["rank"] = i

    conn.close()

    # Output
    out_file = open(args.out, "w", newline="") if args.out else sys.stdout
    fieldnames = ["rank", "model", "provider", "avg_tps", "avg_ttft_ms", "p50_tps", "p95_tps", "total_requests", "total_tokens"]

    if args.csv:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output)
    else:
        # ASCII table
        print(f"{'Rk':>3} {'Model':<40} {'Provider':<20} {'AvgTPS':>8} {'AvgTTFT':>8} {'P50TPS':>8} {'P95TPS':>8} {'Reqs':>6} {'Tokens':>10}")
        print("-" * 120)
        for r in output:
            print(f"{r['rank']:>3} {r['model'][:40]:<40} {r['provider'][:20]:<20} {r['avg_tps']:>8.2f} {r['avg_ttft_ms']:>8.1f} {r['p50_tps']:>8.2f} {r['p95_tps']:>8.2f} {r['total_requests']:>6} {r['total_tokens']:>10}")

    if args.out:
        out_file.close()
        print(f"Written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
