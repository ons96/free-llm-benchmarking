"""Ranking module for free models.

Ranks models by task class (coding, reasoning, long-context, vision, bulk).
Quality first, then speed, then reliability. Cost is zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from freeradar.registry import (
    connect,
    list_models,
    TASK_CLASSES,
    HEALTH_HEALTHY,
    HEALTH_DEGRADED,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEEDRUN_DB = REPO_ROOT / "data" / "speedrun.db"


@dataclass
class RankedModel:
    provider: str
    model_id: str
    task_class: str
    score: float
    quality: float
    speed: float
    reliability: float
    context: int
    modalities: list[str]
    tool_calls: bool
    p50_latency: float | None
    throughput: float | None
    quality_score: float | None
    last_verified: str | None
    health: str
    source_url: str | None
    consecutive_healthy: int
    total_probes: int
    success_count: int


# Task class weights: (quality_weight, speed_weight, reliability_weight)
TASK_WEIGHTS = {
    "coding": (0.50, 0.25, 0.25),
    "reasoning": (0.60, 0.15, 0.25),
    "long-context": (0.40, 0.20, 0.40),
    "vision": (0.55, 0.20, 0.25),
    "bulk": (0.30, 0.50, 0.20),
    "fast": (0.20, 0.70, 0.10),
}


def _load_benchmark_scores() -> dict[str, float]:
    """Load benchmark scores from speedrun.db benchmarks table."""
    if not SPEEDRUN_DB.exists():
        return {}

    conn = sqlite3.connect(str(SPEEDRUN_DB))
    conn.row_factory = sqlite3.Row
    try:
        # Check if benchmarks table exists
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmarks'"
        ).fetchone()
        if not table_exists:
            return {}
        rows = conn.execute(
            "SELECT model_canonical, AVG(score) AS avg_score FROM benchmarks WHERE score IS NOT NULL GROUP BY model_canonical"
        ).fetchall()
    finally:
        conn.close()

    return {r["model_canonical"]: r["avg_score"] for r in rows}


def _normalize_model_name(name: str) -> str:
    """Normalize model name for matching with benchmarks."""
    import re
    n = name.lower().strip()
    prefixes = [
        "openai/", "anthropic/", "google/", "meta/", "mistralai/", "mistral/",
        "deepseek/", "qwen/", "alibaba/", "groq/", "fireworks/", "together/",
        "auto-", "kilocode/",
    ]
    for prefix in prefixes:
        if n.startswith(prefix):
            n = n[len(prefix):]
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    suffix_patterns = [
        r"-\d{8}$", r"-\d{4}-\d{2}-\d{2}$", r"-\d{4}$",
        r"-preview(-\d+)?$", r"-latest$", r":online$", r":free$", r":nitro$",
        r"-instruct$", r"-chat$",
    ]
    for pattern in suffix_patterns:
        n = re.sub(pattern, "", n)
    n = n.replace("_", "-")
    n = re.sub(r"^gpt(\d)", r"gpt-\1", n)
    return n


def _get_quality_score(model_id: str, benchmarks: dict[str, float]) -> float:
    """Get quality score for a model from benchmarks."""
    normalized = _normalize_model_name(model_id)
    if normalized in benchmarks:
        return benchmarks[normalized]

    # Try fuzzy matching
    from difflib import SequenceMatcher
    best_score = 0.0
    best_match = 0.0
    for bench_name, score in benchmarks.items():
        ratio = SequenceMatcher(None, normalized, bench_name).ratio()
        if ratio > best_match and ratio >= 0.8:
            best_match = ratio
            best_score = score
    return best_score


def _compute_speed_score(p50_latency: float | None, throughput: float | None) -> float:
    """Compute speed score from latency and throughput. Balanced caps so faster models
    differentiate but extreme values don't dominate quality/reliability."""
    if p50_latency is None and throughput is None:
        return 0.5

    scores = []
    if p50_latency is not None and p50_latency > 0:
        # 100ms -> 1.0, 50ms -> 1.2 (cap), 200ms -> 0.5, 500ms -> 0.2
        lat_score = min(1.2, 100.0 / p50_latency)
        scores.append(lat_score)
    if throughput is not None and throughput > 0:
        # 100 tps -> 1.0, 150 tps -> 1.5 (cap), 50 tps -> 0.5, 200 tps -> 1.5
        tps_score = min(1.5, throughput / 100.0)
        scores.append(tps_score)

    return sum(scores) / len(scores) if scores else 0.5


def _compute_reliability_score(
    health: str,
    consecutive_healthy: int,
    total_probes: int,
    success_count: int,
) -> float:
    """Compute reliability score from health and probe history."""
    health_scores = {
        HEALTH_HEALTHY: 1.0,
        HEALTH_DEGRADED: 0.5,
        HEALTH_UNKNOWN: 0.3,
        HEALTH_UNHEALTHY: 0.0,
    }

    health_score = health_scores.get(health, 0.3)

    if total_probes > 0:
        success_rate = success_count / total_probes
        # Blend health with success rate
        reliability = 0.6 * health_score + 0.4 * success_rate
    else:
        reliability = health_score * 0.5  # Unknown, penalize

    # Bonus for consecutive healthy probes (up to +0.2)
    streak_bonus = min(0.2, consecutive_healthy * 0.02)
    return min(1.0, reliability + streak_bonus)


def _compute_task_score(
    model: dict[str, Any],
    task_class: str,
    benchmarks: dict[str, float],
) -> tuple[float, float, float, float]:
    """Compute composite score for a task class."""
    weights = TASK_WEIGHTS.get(task_class, TASK_WEIGHTS["coding"])
    q_w, s_w, r_w = weights

    quality = _get_quality_score(model["model_id"], benchmarks)
    speed = _compute_speed_score(model.get("p50_latency"), model.get("throughput"))
    # Normalize speed to [0, 1] range (max possible with caps is 1.5)
    speed = min(1.0, speed / 1.5)
    reliability = _compute_reliability_score(
        model.get("health", HEALTH_UNKNOWN),
        model.get("consecutive_healthy", 0),
        model.get("total_probes", 0),
        model.get("success_count", 0),
    )

    # Use explicit quality_score if available
    if model.get("quality_score") is not None:
        quality = max(quality, model["quality_score"])

    composite = q_w * quality + s_w * speed + r_w * reliability
    return composite, quality, speed, reliability


def _speed_tiebreak(model: dict[str, Any]) -> float:
    """Raw speed used only to break score ties: lower latency, higher throughput wins."""
    latency = float(model.get("p50_latency") or 0.0)
    throughput = float(model.get("throughput") or 0.0)
    if latency <= 0:
        return throughput / 100.0
    return (1000.0 / latency) + (throughput / 100.0)


def rank_models(
    task_class: str,
    top_n: int | None = None,
    db_path: Path | None = None,
    min_health: str = HEALTH_UNKNOWN,
) -> list[RankedModel]:
    """Rank models for a specific task class."""
    if task_class not in TASK_WEIGHTS:
        raise ValueError(f"Unknown task class: {task_class}. Choose from {list(TASK_WEIGHTS.keys())}")

    conn = connect(db_path)
    models = list_models(conn, active_only=True, free_only=True)
    conn.close()

    health_order = {HEALTH_HEALTHY: 0, HEALTH_DEGRADED: 1, HEALTH_UNKNOWN: 2, HEALTH_UNHEALTHY: 3}
    min_order = health_order.get(min_health, 2)
    models = [m for m in models if health_order.get(m.get("health", HEALTH_UNKNOWN), 2) <= min_order]

    benchmarks = _load_benchmark_scores()

    ranked = []
    for m in models:
        # Filter by task suitability
        if task_class == "vision" and "vision" not in m.get("modalities", []):
            continue
        if task_class == "long-context" and m.get("context", 0) < 16384:
            continue
        if task_class in ("coding", "reasoning") and not m.get("tool_calls", False):
            continue  # Tool calls strongly preferred for these

        score, quality, speed, reliability = _compute_task_score(m, task_class, benchmarks)

        ranked.append((RankedModel(
            provider=m["provider"],
            model_id=m["model_id"],
            task_class=task_class,
            score=round(score, 4),
            quality=round(quality, 4),
            speed=round(speed, 4),
            reliability=round(reliability, 4),
            context=m.get("context", 0),
            modalities=m.get("modalities", []),
            tool_calls=m.get("tool_calls", False),
            p50_latency=m.get("p50_latency"),
            throughput=m.get("throughput"),
            quality_score=m.get("quality_score"),
            last_verified=m.get("last_verified"),
            health=m.get("health", HEALTH_UNKNOWN),
            source_url=m.get("source_url"),
            consecutive_healthy=m.get("consecutive_healthy", 0),
            total_probes=m.get("total_probes", 0),
            success_count=m.get("success_count", 0),
        ), _speed_tiebreak(m)))

    ranked.sort(key=lambda entry: (entry[0].score, entry[1]), reverse=True)
    ranked = [entry[0] for entry in ranked]

    if top_n:
        ranked = ranked[:top_n]

    return ranked


def rank_all_tasks(
    top_n: int | None = None,
    db_path: Path | None = None,
) -> dict[str, list[RankedModel]]:
    """Rank models for all task classes."""
    return {task: rank_models(task, top_n, db_path) for task in TASK_WEIGHTS}


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Rank free models by task class")
    parser.add_argument("--task", choices=list(TASK_WEIGHTS.keys()), default="coding")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--db", type=Path, help="Database path")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

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

    sys.exit(0)