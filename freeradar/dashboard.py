"""Dashboard for free-model radar.

Shows top movers, stale entries, provider health, alias history, and 7-day stability report.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from freeradar.registry import (
    connect,
    list_models,
    list_aliases,
    top_movers,
    stale_entries,
    stability_report,
    get_provider_health,
    DB_PATH,
)
from freeradar.aliases import get_alias_status
from freeradar.rank import rank_models, RankedModel

REPO_ROOT = Path(__file__).resolve().parent.parent


def generate_dashboard(
    db_path: Path | None = None,
    days: int = 7,
    top_n: int = 10,
) -> dict[str, Any]:
    """Generate full dashboard data."""
    conn = connect(db_path)

    # Top movers (models with most health/score change)
    movers = top_movers(conn, days)

    # Stale entries
    stale = stale_entries(conn, days)

    # Provider health
    provider_health = []
    for row in conn.execute("SELECT * FROM provider_health ORDER BY updated_at DESC").fetchall():
        provider_health.append(dict(row))

    # Alias status
    alias_status = get_alias_status(db_path)

    # Stability report
    stability = stability_report(conn, days)

    # Current rankings per task
    rankings = {}
    from freeradar.registry import TASK_CLASSES
    for task in list(TASK_CLASSES) + ["fast"]:
        ranked = rank_models(task, top_n=top_n, db_path=db_path)
        rankings[task] = [vars(r) for r in ranked]

    # Model counts
    all_models = list_models(conn, active_only=False, free_only=False)
    active_models = [m for m in all_models if m["status"] == "active"]
    free_models = [m for m in active_models if m["free_tier"]]

    conn.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "summary": {
            "total_models": len(all_models),
            "active_models": len(active_models),
            "free_models": len(free_models),
            "aliases": len(alias_status),
            "providers_monitored": len(provider_health),
        },
        "top_movers": movers[:top_n],
        "stale_entries": [dict(s) for s in stale[:top_n]],
        "provider_health": provider_health,
        "alias_status": alias_status,
        "stability_report": stability,
        "rankings": rankings,
    }


def generate_html_dashboard(
    db_path: Path | None = None,
    days: int = 7,
    top_n: int = 10,
    output_path: Path | None = None,
) -> str:
    """Generate HTML dashboard."""
    data = generate_dashboard(db_path, days, top_n)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Free Model Radar Dashboard</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ color: #333; }}
        h2 {{ color: #555; border-bottom: 2px solid #ddd; padding-bottom: 5px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .card h3 {{ margin-top: 0; color: #666; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .metric {{ font-size: 32px; font-weight: bold; color: #333; }}
        .metric small {{ font-size: 14px; font-weight: normal; color: #999; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #fafafa; font-weight: 600; color: #666; }}
        tr:hover {{ background: #f9f9f9; }}
        .healthy {{ color: #28a745; font-weight: bold; }}
        .degraded {{ color: #ffc107; font-weight: bold; }}
        .unhealthy {{ color: #dc3545; font-weight: bold; }}
        .unknown {{ color: #6c757d; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }}
        .badge-promote {{ background: #d4edda; color: #155724; }}
        .badge-demote {{ background: #f8d7da; color: #721c24; }}
        .badge-keep {{ background: #e2e3e5; color: #383d41; }}
        .badge-init {{ background: #cce5ff; color: #004085; }}
        .rank {{ font-weight: bold; color: #333; }}
        .score-high {{ color: #28a745; }}
        .score-med {{ color: #ffc107; }}
        .score-low {{ color: #dc3545; }}
        .timestamp {{ color: #999; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Free Model Radar Dashboard</h1>
        <p class="timestamp">Generated: {data['generated_at']} | Window: {days} days</p>

        <div class="grid">
            <div class="card">
                <h3>Total Models</h3>
                <div class="metric">{data['summary']['total_models']}<small> ({data['summary']['free_models']} free)</small></div>
            </div>
            <div class="card">
                <h3>Active Models</h3>
                <div class="metric">{data['summary']['active_models']}</div>
            </div>
            <div class="card">
                <h3>Aliases Managed</h3>
                <div class="metric">{data['summary']['aliases']}</div>
            </div>
            <div class="card">
                <h3>Providers Monitored</h3>
                <div class="metric">{data['summary']['providers_monitored']}</div>
            </div>
        </div>

        <h2>Current Aliases</h2>
        <div class="card">
            <table>
                <thead>
                    <tr><th>Alias</th><th>Task</th><th>Current Model</th><th>Score</th><th>Streak</th><th>Health</th><th>Fallback</th><th>Updated</th></tr>
                </thead>
                <tbody>
"""

    for alias, info in data['alias_status'].items():
        health_class = info.get('health', 'unknown').lower() if 'health' in info else 'unknown'
        fallback = f"{info['fallback_provider']}/{info['fallback_model']}" if info.get('fallback_provider') else "none"
        score = info.get('score')
        score_class = "score-high" if score and score > 0.7 else "score-med" if score and score > 0.4 else "score-low"
        html += f"""
                    <tr>
                        <td><strong>{alias}</strong></td>
                        <td>{info['task']}</td>
                        <td>{info['provider']}/{info['model']}</td>
                        <td class="{score_class}">{score:.3f}</td>
                        <td>{info['healthy_streak']}</td>
                        <td><span class="badge badge-{health_class}">{health_class}</span></td>
                        <td>{fallback}</td>
                        <td class="timestamp">{info.get('updated_at', '')[:19] if info.get('updated_at') else 'never'}</td>
                    </tr>"""

    html += """
                </tbody>
            </table>
        </div>

        <h2>Rankings by Task</h2>
        <div class="grid">
"""

    for task, ranked in data['rankings'].items():
        html += f"""
            <div class="card">
                <h3>{task.replace('-', ' ').title()}</h3>
                <table>
                    <thead>
                        <tr><th>#</th><th>Provider</th><th>Model</th><th>Score</th><th>Quality</th><th>Speed</th><th>Reliability</th><th>Context</th><th>Health</th></tr>
                    </thead>
                    <tbody>
"""
        for i, r in enumerate(ranked, 1):
            score_class = "score-high" if r['score'] > 0.7 else "score-med" if r['score'] > 0.4 else "score-low"
            health_class = r['health'].lower()
            html += f"""
                        <tr>
                            <td class="rank">{i}</td>
                            <td>{r['provider']}</td>
                            <td>{r['model_id']}</td>
                            <td class="{score_class}">{r['score']:.3f}</td>
                            <td>{r['quality']:.2f}</td>
                            <td>{r['speed']:.2f}</td>
                            <td>{r['reliability']:.2f}</td>
                            <td>{r['context']}</td>
                            <td><span class="{health_class}">{r['health']}</span></td>
                        </tr>"""
        html += """
                    </tbody>
                </table>
            </div>"""

    html += """
        </div>

        <h2>Top Movers (Last 7 Days)</h2>
        <div class="card">
            <table>
                <thead>
                    <tr><th>Provider</th><th>Model</th><th>Probes</th><th>Successes</th><th>Success Rate</th><th>First Probe</th><th>Last Probe</th></tr>
                </thead>
                <tbody>
"""

    for m in data['top_movers'][:top_n]:
        html += f"""
                    <tr>
                        <td>{m['provider']}</td>
                        <td>{m['model_id']}</td>
                        <td>{m['probes']}</td>
                        <td>{m['successes']}</td>
                        <td>{m['success_rate']:.1%}</td>
                        <td class="timestamp">{m['first_ts'][:19] if m['first_ts'] else ''}</td>
                        <td class="timestamp">{m['last_ts'][:19] if m['last_ts'] else ''}</td>
                    </tr>"""

    html += """
                </tbody>
            </table>
        </div>

        <h2>Stale Entries (Not Verified in 7 Days)</h2>
        <div class="card">
            <table>
                <thead>
                    <tr><th>Provider</th><th>Model</th><th>Last Verified</th><th>Health</th><th>Probes</th></tr>
                </thead>
                <tbody>
"""

    for s in data['stale_entries'][:top_n]:
        health_class = s.get('health', 'unknown').lower()
        html += f"""
                    <tr>
                        <td>{s['provider']}</td>
                        <td>{s['model_id']}</td>
                        <td class="timestamp">{s.get('last_verified', 'never')[:19] if s.get('last_verified') else 'never'}</td>
                        <td><span class="{health_class}">{health_class}</span></td>
                        <td>{s.get('total_probes', 0)}</td>
                    </tr>"""

    html += """
                </tbody>
            </table>
        </div>

        <h2>Provider Health</h2>
        <div class="card">
            <table>
                <thead>
                    <tr><th>Provider</th><th>Health</th><th>Last OK</th><th>Last Error</th><th>Cooldown Until</th><th>Rate Limit Reset</th><th>Free Quota</th><th>Updated</th></tr>
                </thead>
                <tbody>
"""

    for p in data['provider_health']:
        health_class = p.get('health', 'unknown').lower()
        html += f"""
                    <tr>
                        <td>{p['provider']}</td>
                        <td><span class="{health_class}">{health_class}</span></td>
                        <td class="timestamp">{p.get('last_ok', '')[:19] if p.get('last_ok') else 'never'}</td>
                        <td class="timestamp">{p.get('last_error', '')[:19] if p.get('last_error') else 'none'}</td>
                        <td class="timestamp">{p.get('cooldown_until', '')[:19] if p.get('cooldown_until') else 'none'}</td>
                        <td class="timestamp">{p.get('rate_limit_reset_at', '')[:19] if p.get('rate_limit_reset_at') else 'none'}</td>
                        <td>{p.get('free_quota', 'unknown')}</td>
                        <td class="timestamp">{p.get('updated_at', '')[:19] if p.get('updated_at') else ''}</td>
                    </tr>"""

    html += """
                </tbody>
            </table>
        </div>

        <h2>7-Day Stability Report</h2>
        <div class="card">
            <table>
                <thead>
                    <tr><th>Alias</th><th>Model</th><th>Score</th><th>Probes</th><th>Success Rate</th><th>Changes</th><th>Stable</th></tr>
                </thead>
                <tbody>
"""

    for a in data['stability_report']['aliases']:
        stable_badge = '<span class="badge badge-promote">yes</span>' if a['stable'] else '<span class="badge badge-demote">no</span>'
        success_rate = f"{a['success_rate']:.1%}" if a['success_rate'] is not None else "N/A"
        score = a['score'] if a['score'] is not None else 0.0
        html += f"""
                    <tr>
                        <td>{a['alias']}</td>
                        <td>{a['provider']}/{a['model_id']}</td>
                        <td>{score:.3f}</td>
                        <td>{a['probes']}</td>
                        <td>{success_rate}</td>
                        <td>{a['alias_changes']}</td>
                        <td>{stable_badge}</td>
                    </tr>"""

    html += f"""
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""

    if output_path:
        output_path.write_text(html)
        return str(output_path)

    return html


if __name__ == "__main__":
    import argparse
    import sys

    from freeradar.registry import DB_PATH as DEFAULT_DB_PATH

    parser = argparse.ArgumentParser(description="Generate free-model radar dashboard")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Database path")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--html", type=Path, help="Output HTML file")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    if args.html:
        path = generate_html_dashboard(args.db, args.days, args.top, args.html)
        print(f"Dashboard written to {path}")
    elif args.json:
        data = generate_dashboard(args.db, args.days, args.top)
        print(json.dumps(data, indent=2, default=str))
    else:
        data = generate_dashboard(args.db, args.days, args.top)
        print(json.dumps(data, indent=2, default=str))

    sys.exit(0)