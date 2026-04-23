#!/usr/bin/env python3
"""llm-speedrun: benchmark LLM providers for speed + quality."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import db
from config import load_all_targets, Target


def cmd_init(args):
    db.init_db()
    print(f"Database initialized at {db.DB_PATH}")


def cmd_list(args):
    """List targets that would be tested."""
    targets = load_all_targets(
        include_credits=args.include_credits,
        include_expensive=args.include_expensive,
        include_paid=args.include_paid,
        provider_filter=args.provider,
        model_filter=args.model,
    )

    by_provider: dict[str, list[Target]] = {}
    for t in targets:
        by_provider.setdefault(t.provider_name, []).append(t)

    print(f"\nTotal: {len(targets)} targets across {len(by_provider)} providers\n")
    for provider, t_list in sorted(by_provider.items()):
        reasoning = sum(1 for t in t_list if t.supports_reasoning)
        print(f"  {provider} ({t_list[0].base_url})")
        print(f"    {len(t_list)} models, {reasoning} reasoning-capable")
        if args.verbose:
            for t in t_list:
                tag = " [R]" if t.supports_reasoning else ""
                print(f"      - {t.model_name}{tag}")


def cmd_test(args):
    """Run speed tests."""
    import sqlite3
    from runner import run_all

    targets = load_all_targets(
        include_credits=args.include_credits,
        include_expensive=args.include_expensive,
        include_paid=args.include_paid,
        provider_filter=args.provider,
        model_filter=args.model,
    )

    # Filter out already-tested models if requested
    if args.skip_tested:
        conn = sqlite3.connect(str(Path(__file__).parent / "data" / "speedrun.db"))
        cur = conn.execute(
            "SELECT provider_name, model_name FROM speed_tests WHERE status = 'success'"
        )
        tested = set((r[0], r[1]) for r in cur.fetchall())
        conn.close()
        targets = [t for t in targets if (t.provider_name, t.model_name) not in tested]
        print(f"Skipped {len(tested)} already-tested models.")

    # Retest models with 429/403 errors
    if args.retry_errors:
        conn = sqlite3.connect(str(Path(__file__).parent / "data" / "speedrun.db"))
        cur = conn.execute("""
            SELECT provider_name, model_name FROM speed_tests
            WHERE status = 'http_error' AND (
                error_message LIKE '%429%' OR
                error_message LIKE '%403%' OR
                error_message LIKE '%rate%limit%' OR
                error_message LIKE '%credit%'
            )
        """)
        error_models = set((r[0], r[1]) for r in cur.fetchall())
        conn.close()
        targets = [
            t for t in targets if (t.provider_name, t.model_name) in error_models
        ]
        print(f"Retesting {len(error_models)} models with rate/credit errors.")

    if not targets:
        print("No targets matched filters.")
        return

    # Estimate cost
    reasoning_count = sum(1 for t in targets if t.supports_reasoning)
    effort_multiplier = 3 if args.effort_sweep else 1
    total_calls = (
        len(targets) - reasoning_count
    ) * args.runs + reasoning_count * args.runs * effort_multiplier
    est_tokens = total_calls * 220  # ~20 input + 200 output
    est_minutes = (total_calls * 3) / 60  # rough: 3s avg per call

    print(f"\n=== Speed Test Plan ===")
    print(f"Targets: {len(targets)} models ({reasoning_count} reasoning)")
    print(f"Runs per target: {args.runs}")
    print(f"Effort sweep: {args.effort_sweep}")
    print(f"Total API calls: ~{total_calls}")
    print(f"Estimated tokens used: ~{est_tokens:,}")
    print(
        f"Estimated duration: ~{est_minutes:.1f} min (concurrency={args.concurrency})"
    )

    if not args.yes:
        confirm = input("\nProceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    db.init_db()

    done = [0]
    checkpoint = [0]

    def on_progress(d, total, target, effort, results):
        done[0] = d
        if checkpoint[0] == 0 or d - checkpoint[0] >= 20:
            checkpoint[0] = d
            if handler.run_id:
                export_csv(handler.run_id)
        ok = sum(1 for r in results if r.status == "success")
        ok = sum(1 for r in results if r.status == "success")
        avg_ttft = None
        avg_tps = None
        successful = [r for r in results if r.status == "success"]
        if successful:
            ttfts = [r.ttft_sec for r in successful if r.ttft_sec]
            tps_vals = [r.tps for r in successful if r.tps]
            avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None
            avg_tps = sum(tps_vals) / len(tps_vals) if tps_vals else None

        eff_tag = f" [{effort}]" if effort else ""
        status_tag = f"{ok}/{len(results)}"
        metrics = ""
        if avg_ttft is not None:
            metrics = f"  TTFT {avg_ttft:.3f}s"
            if avg_tps:
                metrics += f"  TPS {avg_tps:.1f}"
        print(
            f"[{d}/{total}] {target.provider_name}/{target.model_name}{eff_tag}  {status_tag}{metrics}"
        )

    # Auto-export raw CSV after completion (and on Ctrl+C via try/finally)
    def export_csv(run_id: str) -> None:
        import csv

        conn = db.connect()
        try:
            cur = conn.execute(
                """
                SELECT provider_name, provider_url, model_name, source, reasoning_effort,
ttft_sec, tps, output_tokens, total_time_sec, status, error_message, timestamp
                 FROM speed_tests WHERE run_id = ?
            """,
                (run_id,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        output = Path("data") / f"raw_tests_{run_id}.csv"
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "provider",
                    "url",
                    "model",
                    "source",
                    "effort",
                    "ttft_sec",
                    "tps",
                    "tokens",
                    "total_sec",
                    "status",
                    "error",
                    "timestamp",
                ]
            )
            for r in rows:
                writer.writerow(r)
        print(f"Exported {len(rows)} rows to {output}")

    import signal

    class SigintHandler:
        run_id = None

    handler = SigintHandler()

    def handle_sigint(signum, frame):
        print("\n\nInterrupted! Saving results...")
        if handler.run_id:
            export_csv(handler.run_id)
        sys.exit(0)

    orig_handler = signal.signal(signal.SIGINT, handle_sigint)

    print(f"\n=== Running ===\n")
    try:
        run_id = asyncio.run(
            run_all(
                targets,
                num_runs=args.runs,
                reasoning_effort=args.reasoning_effort,
                max_concurrent=args.concurrency,
                effort_sweep=args.effort_sweep,
                on_progress=on_progress,
            )
        )
        handler.run_id = run_id
        print(f"\n=== Run complete ===")
        print(f"Run ID: {run_id}")
        print(f"Use `python cli.py report` to see results")
    finally:
        if handler.run_id:
            export_csv(handler.run_id)
        signal.signal(signal.SIGINT, orig_handler)


def cmd_fetch(args):
    """Fetch external benchmark data."""
    from benchmarks import fetch_all, fetch_source

    db.init_db()

    if args.source:
        print(f"Fetching {args.source}...")
        rows = fetch_source(args.source, refresh=args.refresh)
        print(f"  → {len(rows)} models")
        # Store in DB
        from matcher import normalize, extract_reasoning_effort

        conn = db.connect()
        for row in rows:
            model_name = row.get("model", "")
            name_clean, effort = extract_reasoning_effort(model_name)
            canonical = normalize(name_clean)
            db.upsert_benchmark(
                conn,
                model_canonical=canonical,
                benchmark_source=args.source,
                reasoning_effort=effort,
                score=row.get("score"),
                score_label=row.get("score_label", ""),
                raw_data=row,
            )
        conn.commit()
        conn.close()
    else:
        print("Fetching all benchmark sources...")
        results = fetch_all(refresh=args.refresh)
        total = sum(len(v) for v in results.values())
        print(f"\nTotal benchmark entries: {total}")


def cmd_raw_csv(args):
    """Export all raw test data to CSV."""
    import csv
    from db import connect

    conn = connect()
    cur = conn.execute("""
        SELECT provider_name, provider_url, model_name, source, reasoning_effort,
ttft_sec, tps, output_tokens, total_time_sec, status, error_message, timestamp
         FROM speed_tests ORDER BY timestamp DESC
    """)
    rows = cur.fetchall()
    conn.close()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "provider",
                "url",
                "model",
                "source",
                "effort",
                "ttft_sec",
                "tps",
                "tokens",
                "total_sec",
                "status",
                "error",
                "timestamp",
            ]
        )
        for r in rows:
            writer.writerow(r)
    print(f"Exported {len(rows)} rows to {output}")


def cmd_csv(args):
    """Export results to CSV."""
    import csv
    from db import connect
    from ranker import compute_rankings

    rankings = compute_rankings(args.speed_weight, args.quality_weight)
    if not rankings:
        print("No test results found. Run `python cli.py test` first.")
        return

    output = Path(args.output) if args.output else Path("data") / "leaderboard.csv"
    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "rank",
                "provider",
                "model",
                "effort",
                "TTFT_sec",
                "TPS",
                "avg_tokens",
                "10K_total_sec",
            ]
        )
        for r in rankings:
            ttft_sec = r["avg_ttft_sec"] if r["avg_ttft_sec"] else ""
            t10k = r["est_10k_total_s"] if r["est_10k_total_s"] else ""
            writer.writerow(
                [
                    r["rank"],
                    r["provider_name"],
                    r["model_name"],
                    r.get("reasoning_effort", ""),
                    f"{ttft_sec:.3f}" if ttft_sec else "",
                    f"{r['avg_tps']:.1f}" if r["avg_tps"] else "",
                    f"{r.get('avg_output_tokens', 0):.0f}",
                    f"{t10k:.1f}" if t10k else "",
                ]
            )
    print(f"Exported to {output}")


def cmd_report(args):
    """Display ranked leaderboard."""
    from ranker import compute_rankings, rankings_by_model

    if args.by_model:
        by_model = rankings_by_model(args.speed_weight, args.quality_weight)
        for canonical, rows in sorted(by_model.items()):
            if len(rows) < 2 and not args.all:
                continue
            print(f"\n== {canonical} ({len(rows)} providers) ==")
            for r in rows[:10]:
                ttft = f"{r['avg_ttft_sec']:.3f}s" if r["avg_ttft_sec"] else "?"
                tps = f"{r['avg_tps']:.1f}" if r["avg_tps"] else "?"
                t10k = f"{r['est_10k_total_s']:.1f}s" if r["est_10k_total_s"] else "?"
                print(
                    f"  #{r['rank']:3}  {r['provider_name']:12} {r['model_name']:45}  "
                    f"TTFT={ttft:8}  TPS={tps:6}  10K={t10k}"
                )
        return

    rankings = compute_rankings(args.speed_weight, args.quality_weight)
    if not rankings:
        print("No test results found. Run `python cli.py test` first.")
        return

    print(f"\n=== Speed Leaderboard (top {args.top}) ===")
    print(f"Weights: speed={args.speed_weight} quality={args.quality_weight}\n")

    fmt = "{rank:>4}  {prov:12} {model:45} {effort:7} {ttft:>8} {tps:>7} {t10k:>7} {bench:>6}"
    print(
        fmt.format(
            rank="#",
            prov="provider",
            model="model",
            effort="effort",
            ttft="TTFT",
            tps="TPS",
            t10k="10K(s)",
            bench="bench",
        )
    )
    print("-" * 110)

    for r in rankings[: args.top]:
        ttft = f"{r['avg_ttft_sec']:.3f}s" if r["avg_ttft_sec"] else "?"
        tps = f"{r['avg_tps']:.1f}" if r["avg_tps"] else "?"
        t10k = f"{r['est_10k_total_s']:.1f}" if r["est_10k_total_s"] else "?"
        bench = f"{r['bench_score']:.1f}" if r.get("bench_score") else "-"
        effort = r.get("reasoning_effort") or ""
        print(
            fmt.format(
                rank=r["rank"],
                prov=(r["provider_name"] or "")[:12],
                model=(r["model_name"] or "")[:45],
                effort=effort[:7],
                ttft=ttft,
                tps=tps,
                t10k=t10k,
                bench=bench,
            )
        )

    if args.json:
        out_path = Path(args.json)
        out_path.write_text(json.dumps(rankings[: args.top], indent=2, default=str))
        print(f"\nWrote {out_path}")


def cmd_apply(args):
    """Patch gateway virtual_models.yaml with new rankings."""
    from patcher import patch_gateway_config

    print(f"=== Gateway Patch (dry_run={args.dry_run}) ===")
    result = patch_gateway_config(
        speed_weight=args.speed_weight,
        quality_weight=args.quality_weight,
        dry_run=args.dry_run,
        backup=True,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    print(f"\nChanges: {result['changes_count']} virtual models reordered\n")
    for ch in result["changes"][:20]:
        print(f"  [{ch['virtual_model']}]")
        print(f"    before: {ch['before']}")
        print(f"    after:  {ch['after']}")
        print()

    if not args.dry_run:
        print(f"Backup: {result.get('backup')}")
        print(f"Written: {result.get('written')}")
    else:
        print("(Dry run - no changes written. Pass --write to apply.)")


def main():
    parser = argparse.ArgumentParser(prog="llm-speedrun", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize database")
    p_init.set_defaults(func=cmd_init)

    # list
    p_list = sub.add_parser("list", help="List targets that would be tested")
    p_list.add_argument("--provider", help="Glob filter for provider name")
    p_list.add_argument("--model", help="Glob filter for model name")
    p_list.add_argument(
        "--include-credits", action="store_true", help="Include ktai-paid"
    )
    p_list.add_argument("--include-expensive", action="store_true")
    p_list.add_argument(
        "--include-paid",
        action="store_true",
        help="Include paid models (not just free ones)",
    )
    p_list.add_argument("-v", "--verbose", action="store_true")
    p_list.set_defaults(func=cmd_list)

    # test
    p_test = sub.add_parser("test", help="Run speed tests against LLMs")
    p_test.add_argument("--provider", help="Glob filter for provider name")
    p_test.add_argument("--model", help="Glob filter for model name")
    p_test.add_argument("--include-credits", action="store_true")
    p_test.add_argument("--include-expensive", action="store_true")
    p_test.add_argument(
        "--include-paid",
        action="store_true",
        help="Include paid models (not just free ones)",
    )
    p_test.add_argument("--runs", type=int, default=3)
    p_test.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["low", "medium", "high", "minimal"],
    )
    p_test.add_argument(
        "--effort-sweep",
        action="store_true",
        help="Test low/medium/high for reasoning models",
    )
    p_test.add_argument("--concurrency", type=int, default=4)
    p_test.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_test.add_argument(
        "--skip-tested", action="store_true", help="Skip already tested models"
    )
    p_test.add_argument(
        "--retry-errors", action="store_true", help="Retest models with 429/403 errors"
    )
    p_test.set_defaults(func=cmd_test)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch external benchmark data")
    p_fetch.add_argument(
        "--source", choices=["aider", "livebench", "lmarena", "swebench"]
    )
    p_fetch.add_argument("--refresh", action="store_true", help="Force refresh cache")
    p_fetch.set_defaults(func=cmd_fetch)

    # report
    p_report = sub.add_parser("report", help="Show ranked leaderboard")
    p_report.add_argument("--top", type=int, default=50)
    p_report.add_argument("--speed-weight", type=float, default=0.5)
    p_report.add_argument("--quality-weight", type=float, default=0.5)
    p_report.add_argument(
        "--by-model",
        action="store_true",
        help="Group by canonical model, compare providers",
    )
    p_report.add_argument(
        "--all",
        action="store_true",
        help="Show all models in by-model view (default: only multi-provider)",
    )
    p_report.add_argument("--json", help="Also write results to JSON file")
    p_report.set_defaults(func=cmd_report)

    # csv
    p_csv = sub.add_parser("csv", help="Export results to CSV")
    p_csv.add_argument("--top", type=int, default=0, help="Limit results (0=all)")
    p_csv.add_argument("--speed-weight", type=float, default=0.5)
    p_csv.add_argument("--quality-weight", type=float, default=0.5)
    p_csv.add_argument("--output", "-o", help="Output CSV path")
    p_csv.set_defaults(func=cmd_csv)

    # raw-csv - export all raw test data
    p_raw_csv = sub.add_parser("raw-csv", help="Export raw test data to CSV")
    p_raw_csv.add_argument(
        "--output", "-o", default="data/raw_tests.csv", help="Output CSV path"
    )
    p_raw_csv.set_defaults(func=cmd_raw_csv)

    # apply
    p_apply = sub.add_parser("apply", help="Patch gateway virtual_models.yaml")
    p_apply.add_argument("--speed-weight", type=float, default=0.5)
    p_apply.add_argument("--quality-weight", type=float, default=0.5)
    p_apply.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="Actually write changes (default: dry run)",
    )
    p_apply.set_defaults(func=cmd_apply, dry_run=True)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
