"""Main entry point for free-model radar daily run.

Runs discovery, verification, ranking, and alias updates.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from freeradar.registry import init_db, export_json, DB_PATH, JSON_PATH
from freeradar.discovery import run_discovery_sync
from freeradar.verify import verify_all_sync
from freeradar.aliases import compute_alias_proposals, apply_alias_proposals
from freeradar.dashboard import generate_html_dashboard


def run_daily(
    db_path: Path | None = None,
    json_path: Path | None = None,
    html_path: Path | None = None,
    skip_discovery: bool = False,
    skip_verification: bool = False,
    skip_aliases: bool = False,
    apply_aliases: bool = False,
    max_verify: int | None = None,
    dry_run_aliases: bool = True,
) -> dict:
    """Run the full daily radar pipeline."""
    db_path = db_path or DB_PATH
    json_path = json_path or JSON_PATH
    html_path = html_path or (db_path.parent / "dashboard.html")

    init_db(db_path)

    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "discovery": {},
        "verification": {},
        "aliases": {},
        "dashboard": str(html_path),
    }

    # Discovery
    if not skip_discovery:
        print("Running discovery...")
        discovery_counts = run_discovery_sync(db_path)
        result["discovery"] = discovery_counts
        print(f"  Discovery counts: {discovery_counts}")

    # Verification
    if not skip_verification:
        print("Running verification...")
        verify_results = verify_all_sync(db_path, limit=max_verify)
        result["verification"] = {
            "models_probed": len(verify_results),
            "details": {k: {pk: vars(pv) for pk, pv in v.items()} for k, v in verify_results.items()},
        }
        print(f"  Verified {len(verify_results)} models")

    # Aliases
    if not skip_aliases:
        print("Computing alias proposals...")
        proposals = compute_alias_proposals(db_path)
        result["aliases"]["proposals"] = [vars(p) for p in proposals]

        if apply_aliases or not dry_run_aliases:
            print("Applying aliases...")
            alias_result = apply_alias_proposals(proposals, db_path, dry_run=dry_run_aliases)
            result["aliases"]["apply_result"] = alias_result
            print(f"  Applied {len(alias_result.get('changes', []))} changes")
        else:
            print("  Dry-run only (use --apply-aliases to write)")

    # Export JSON
    print("Exporting JSON...")
    from freeradar.registry import connect
    conn = connect(db_path)
    export_json(conn, json_path, db_path)
    conn.close()
    print(f"  Exported to {json_path}")

    # Generate HTML dashboard
    print("Generating dashboard...")
    generate_html_dashboard(db_path, output_path=html_path)
    print(f"  Dashboard at {html_path}")

    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description="Free-model radar daily run")
    parser.add_argument("--db", type=Path, help="Database path")
    parser.add_argument("--json-out", type=Path, help="JSON output path")
    parser.add_argument("--html-out", type=Path, help="HTML dashboard output path")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-verification", action="store_true")
    parser.add_argument("--skip-aliases", action="store_true")
    parser.add_argument("--apply-aliases", action="store_true", help="Actually apply alias changes")
    parser.add_argument("--dry-run-aliases", action="store_true", default=True, help="Dry-run alias changes (default)")
    parser.add_argument("--max-verify", type=int, help="Max models to verify")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    result = run_daily(
        db_path=args.db,
        json_path=args.json_out,
        html_path=args.html_out,
        skip_discovery=args.skip_discovery,
        skip_verification=args.skip_verification,
        skip_aliases=args.skip_aliases,
        apply_aliases=args.apply_aliases,
        max_verify=args.max_verify,
        dry_run_aliases=args.dry_run_aliases,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())