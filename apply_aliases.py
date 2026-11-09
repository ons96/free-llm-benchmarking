#!/usr/bin/env python3
"""Apply gateway alias changes for free models.

Usage:
    python apply_aliases.py --dry-run
    python apply_aliases.py --apply --backup
"""

import argparse
import json
import sys
from pathlib import Path

from freeradar.aliases import compute_alias_proposals, apply_alias_proposals, MIN_CONSECUTIVE_HEALTHY
from freeradar.registry import DB_PATH as DEFAULT_DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Manage gateway aliases for free models")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Show proposed changes without applying (default)")
    parser.add_argument("--apply", dest="dry_run", action="store_false", help="Apply changes to gateway")
    parser.add_argument("--no-backup", dest="backup", action="store_false", help="Skip backup")
    parser.add_argument("--min-streak", type=int, default=MIN_CONSECUTIVE_HEALTHY, help="Min consecutive healthy probes")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Database path")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.set_defaults(dry_run=True, backup=True)
    args = parser.parse_args()

    proposals = compute_alias_proposals(args.db, args.min_streak)

    if args.json:
        print(json.dumps([vars(p) for p in proposals], indent=2, default=str))
    else:
        print(f"\n=== Alias Proposals (dry_run={args.dry_run}) ===")
        for p in proposals:
            print(f"\n  {p.alias} ({p.task})")
            print(f"    Action: {p.action}")
            if p.current_provider:
                print(f"    Current: {p.current_provider}/{p.current_model}")
            else:
                print(f"    Current: none")
            if p.proposed_provider:
                print(f"    Proposed: {p.proposed_provider}/{p.proposed_model}")
            else:
                print(f"    Proposed: none")
            if p.score is not None:
                print(f"    Score: {p.score:.3f}")
            print(f"    Streak: {p.healthy_streak}")
            if p.fallback_provider:
                print(f"    Fallback: {p.fallback_provider}/{p.fallback_model}")
            else:
                print(f"    Fallback: none")
            print(f"    Reason: {p.reason}")

    if not args.dry_run:
        result = apply_alias_proposals(proposals, args.db, dry_run=False, backup=args.backup)
        print(f"\n=== Applied {len(result['changes'])} changes ===")
        for ch in result["changes"]:
            print(f"  [{ch['alias']}] {ch['action']}: {ch['from']} -> {ch['to']} (score={ch['score']:.3f})")
        if result["backup"]:
            print(f"Backup: {result['backup']}")
        if args.json:
            print(json.dumps(result, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())