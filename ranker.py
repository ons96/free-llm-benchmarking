"""Combine speed test results with benchmark scores to produce rankings."""

import sqlite3
from typing import Optional

import db
from matcher import normalize, match_model


def get_latest_speed(conn: sqlite3.Connection) -> list[dict]:
    """Get latest speed summary for each provider/model/effort combo."""
    rows = conn.execute("""
        SELECT s.*
        FROM speed_summary s
        INNER JOIN (
            SELECT provider_name, model_name, reasoning_effort, MAX(timestamp) AS max_ts
            FROM speed_summary
            GROUP BY provider_name, model_name, reasoning_effort
        ) latest
        ON s.provider_name = latest.provider_name
         AND s.model_name = latest.model_name
         AND (s.reasoning_effort IS latest.reasoning_effort
              OR s.reasoning_effort = latest.reasoning_effort)
         AND s.timestamp = latest.max_ts
        WHERE s.num_success > 0
        ORDER BY s.est_10k_total_s ASC
    """).fetchall()
    return [dict(r) for r in rows]


def get_benchmarks(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Get benchmark scores keyed by canonical model name.
    Returns {canonical: {source: score}}.
    """
    rows = conn.execute(
        "SELECT model_canonical, benchmark_source, score FROM benchmarks WHERE score IS NOT NULL"
    ).fetchall()

    out: dict[str, dict[str, float]] = {}
    for r in rows:
        canon = r["model_canonical"]
        if canon not in out:
            out[canon] = {}
        out[canon][r["benchmark_source"]] = r["score"]
    return out


def compute_rankings(
    speed_weight: float = 0.5,
    quality_weight: float = 0.5,
    benchmark_source: Optional[str] = None,
) -> list[dict]:
    """Produce ranked list combining speed + quality."""
    conn = db.connect()
    speed_rows = get_latest_speed(conn)
    bench_data = get_benchmarks(conn)
    conn.close()

    if not speed_rows:
        return []

    # Get all benchmark canonical names for matching
    bench_canonicals = list(bench_data.keys())

    # Annotate speed rows with benchmark scores
    for row in speed_rows:
        canonical = normalize(row["model_name"])
        row["canonical"] = canonical

        # Try matching
        matched, confidence, method = match_model(canonical, bench_canonicals)
        if matched and confidence >= 0.7:
            scores = bench_data[matched]
            row["bench_match"] = matched
            row["bench_confidence"] = confidence
            row["bench_method"] = method

            if benchmark_source and benchmark_source in scores:
                row["bench_score"] = scores[benchmark_source]
            else:
                # Use average of available benchmarks (normalized)
                vals = list(scores.values())
                row["bench_score"] = sum(vals) / len(vals) if vals else None
        else:
            row["bench_match"] = None
            row["bench_score"] = None
            row["bench_confidence"] = 0
            row["bench_method"] = "none"

    # Compute percentiles
    # Speed: lower est_10k_total_s = better = higher percentile
    speeds = [
        r["est_10k_total_s"] for r in speed_rows if r["est_10k_total_s"] is not None
    ]
    if speeds:
        max_speed = max(speeds)
        min_speed = min(speeds)
        speed_range = max_speed - min_speed if max_speed > min_speed else 1.0

    bench_scores = [
        r["bench_score"] for r in speed_rows if r["bench_score"] is not None
    ]
    if bench_scores:
        max_bench = max(bench_scores)
        min_bench = min(bench_scores)
        bench_range = max_bench - min_bench if max_bench > min_bench else 1.0

    for row in speed_rows:
        # Speed percentile (1.0 = fastest)
        if row["est_10k_total_s"] is not None and speeds:
            row["speed_pct"] = 1.0 - (row["est_10k_total_s"] - min_speed) / speed_range
        else:
            row["speed_pct"] = 0.0

        # Quality percentile (1.0 = highest benchmark)
        if row["bench_score"] is not None and bench_scores:
            row["quality_pct"] = (row["bench_score"] - min_bench) / bench_range
        else:
            row["quality_pct"] = None

        # Composite
        if row["quality_pct"] is not None:
            row["composite"] = (
                speed_weight * row["speed_pct"] + quality_weight * row["quality_pct"]
            )
        else:
            # Speed-only if no benchmark
            row["composite"] = row["speed_pct"]

    # Sort by composite descending
    speed_rows.sort(key=lambda r: r["composite"], reverse=True)
    for i, row in enumerate(speed_rows):
        row["rank"] = i + 1

    return speed_rows


def rankings_by_model(
    speed_weight: float = 0.5,
    quality_weight: float = 0.5,
) -> dict[str, list[dict]]:
    """Group rankings by canonical model, showing provider comparison."""
    ranked = compute_rankings(speed_weight, quality_weight)
    by_model: dict[str, list[dict]] = {}
    for row in ranked:
        canon = row.get("canonical", "unknown")
        if canon not in by_model:
            by_model[canon] = []
        by_model[canon].append(row)
    return by_model
