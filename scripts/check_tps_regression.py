#!/usr/bin/env python3
"""Detect week-over-week TPS regressions and alert via task-board issue.

Compares the most recent avg_tps per (provider, model, effort) against the
average from the prior comparison window (default 7 days earlier). If TPS
dropped more than the threshold (default 50%), posts a single aggregated
issue to ons96/task-board via the gh CLI (label tag:monitoring).

Designed for the VPS nightly cron. Degrades gracefully:
- No gh CLI or gh failure -> logs to stderr, exit 0 (cron must not fail).
- Insufficient history -> logs and exits 0.

Usage:
    python3 scripts/check_tps_regression.py [--db data/speedrun.db]
        [--threshold 0.5] [--window-days 7] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = "ons96/task-board"
ISSUE_LABELS = ["tag:monitoring", "status:new", "priority:P2",
                "project:free-llm-benchmarking"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/speedrun.db", help="Path to speedrun.db")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="Fractional TPS drop to alert on (0.5 = 50%%)")
    p.add_argument("--window-days", type=int, default=7,
                   help="Days back for the baseline window")
    p.add_argument("--min-tps", type=float, default=5.0,
                   help="Ignore models whose baseline TPS is below this")
    p.add_argument("--dry-run", action="store_true",
                   help="Print regressions, do not post an issue")
    return p.parse_args()


def fetch_regressions(db_path: Path, threshold: float, window_days: int,
                      min_tps: float) -> list[dict]:
    """Return rows where current avg_tps dropped > threshold vs baseline."""
    now = dt.datetime.now(dt.timezone.utc)
    recent_start = (now - dt.timedelta(days=2)).isoformat()
    base_end = (now - dt.timedelta(days=window_days - 2)).isoformat()
    base_start = (now - dt.timedelta(days=window_days + 7)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH recent AS (
                SELECT provider_name, model_name,
                       COALESCE(reasoning_effort, '') AS effort,
                       AVG(avg_tps) AS tps
                FROM speed_summary
                WHERE timestamp >= ? AND avg_tps > 0 AND num_success > 0
                GROUP BY 1, 2, 3
            ),
            baseline AS (
                SELECT provider_name, model_name,
                       COALESCE(reasoning_effort, '') AS effort,
                       AVG(avg_tps) AS tps
                FROM speed_summary
                WHERE timestamp >= ? AND timestamp < ?
                      AND avg_tps > 0 AND num_success > 0
                GROUP BY 1, 2, 3
            )
            SELECT r.provider_name, r.model_name, r.effort,
                   b.tps AS baseline_tps, r.tps AS recent_tps
            FROM recent r
            JOIN baseline b
              ON r.provider_name = b.provider_name
             AND r.model_name = b.model_name
             AND r.effort = b.effort
            WHERE b.tps >= ?
              AND r.tps < b.tps * (1.0 - ?)
            ORDER BY (b.tps - r.tps) / b.tps DESC
            """,
            (recent_start, base_start, base_end, min_tps, threshold),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def has_history(db_path: Path, window_days: int) -> bool:
    """True when the DB has summary rows older than the baseline window."""
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=window_days - 2)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM speed_summary WHERE timestamp < ?",
            (cutoff,),
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row[0] > 0)


def format_issue(regressions: list[dict], threshold: float) -> tuple[str, str]:
    today = dt.date.today().isoformat()
    title = (f"TPS regression alert {today}: {len(regressions)} model(s) "
             f"dropped >{int(threshold * 100)}%")
    lines = [
        "Automated alert from llm-speedrun cron "
        "(scripts/check_tps_regression.py).",
        "",
        "| Provider | Model | Effort | Baseline TPS | Recent TPS | Drop |",
        "|---|---|---|---|---|---|",
    ]
    for r in regressions:
        drop = (r["baseline_tps"] - r["recent_tps"]) / r["baseline_tps"]
        lines.append(
            f"| {r['provider_name']} | {r['model_name']} | "
            f"{r['effort'] or '-'} | {r['baseline_tps']:.1f} | "
            f"{r['recent_tps']:.1f} | {drop:.0%} |"
        )
    lines += [
        "",
        "Possible causes: provider throttling, model rerouting, "
        "gateway issues, or measurement changes.",
        "",
        "## Scope",
        "cross-device",
    ]
    return title, "\n".join(lines)


def post_issue(title: str, body: str) -> bool:
    gh = shutil.which("gh")
    if not gh:
        print("check_tps_regression: gh CLI not found; logging only",
              file=sys.stderr)
        return False
    cmd = [gh, "issue", "create", "--repo", REPO, "--title", title,
           "--body", body]
    for label in ISSUE_LABELS:
        cmd += ["--label", label]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"check_tps_regression: gh issue create failed: {exc}",
              file=sys.stderr)
        return False
    if res.returncode != 0:
        print(f"check_tps_regression: gh error: {res.stderr.strip()}",
              file=sys.stderr)
        return False
    print(f"Posted regression issue: {res.stdout.strip()}")
    return True


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"check_tps_regression: DB not found: {db_path}",
              file=sys.stderr)
        return 0

    if not has_history(db_path, args.window_days):
        print("check_tps_regression: insufficient history for "
              f"{args.window_days}-day comparison; skipping")
        return 0

    regressions = fetch_regressions(db_path, args.threshold,
                                    args.window_days, args.min_tps)
    if not regressions:
        print("check_tps_regression: no regressions detected")
        return 0

    title, body = format_issue(regressions, args.threshold)
    print(f"check_tps_regression: {len(regressions)} regression(s) detected")
    if args.dry_run:
        print(title)
        print(body)
        return 0

    post_issue(title, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
