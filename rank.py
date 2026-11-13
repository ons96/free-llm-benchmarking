#!/usr/bin/env python3
"""Rank free models by task class.

Usage:
    python rank.py --task coding --top 10
    python rank.py --task reasoning --top 20 --json
"""

import argparse
import json
import sys
from pathlib import Path

from freeradar.rank import rank_models, rank_all_tasks, TASK_CLASSES
from freeradar.registry import DB_PATH as DEFAULT_DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Rank free models by task class")
    parser.add_argument(
        "--task",
        choices=list(TASK_CLASSES) + ["fast"],
        default="coding",
        help="Task class to rank for",
    )
    parser.add_argument("--top", type=int, default=10, help="Number of top models to show")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Database path (default: data/free_models.db)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--all-tasks", action="store_true", help="Rank for all task classes")
    args = parser.parse_args()

    if args.all_tasks:
        results = rank_all_tasks(args.top, args.db)
        if args.json:
            print(json.dumps({k: [vars(r) for r in v] for k, v in results.items()}, indent=2, default=str))
        else:
            for task, ranked in results.items():
                print(f"\n=== Top {len(ranked)} for {task} ===")
                print(f"{'Rank':>4}  {'Provider':15} {'Model':45} {'Score':>6} {'Qual':>5} {'Speed':>5} {'Rel':>5} {'Ctx':>6} {'Health':>10} {'Verified':>20}")
                print("-" * 140)
                for i, r in enumerate(ranked, 1):
                    verified = r.last_verified[:19] if r.last_verified else "never"
                    print(f"{i:>4}  {r.provider:15} {r.model_id:45} {r.score:>6.3f} {r.quality:>5.2f} {r.speed:>5.2f} {r.reliability:>5.2f} {r.context:>6} {r.health:>10} {verified:>20}")
        return 0

    results = rank_models(args.task, args.top, args.db)

    if args.json:
        print(json.dumps([vars(r) for r in results], indent=2, default=str))
    else:
        print(f"\n=== Top {len(results)} for {args.task} ===")
        print(f"{'Rank':>4}  {'Provider':15} {'Model':45} {'Score':>6} {'Qual':>5} {'Speed':>5} {'Rel':>5} {'Ctx':>6} {'Health':>10} {'Verified':>20}")
        print("-" * 140)
        for i, r in enumerate(results, 1):
            verified = r.last_verified[:19] if r.last_verified else "never"
            print(f"{i:>4}  {r.provider:15} {r.model_id:45} {r.score:>6.3f} {r.quality:>5.2f} {r.speed:>5.2f} {r.reliability:>5.2f} {r.context:>6} {r.health:>10} {verified:>20}")

    return 0


if __name__ == "__main__":
    sys.exit(main())