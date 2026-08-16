"""Generate ranked provider fallback chains from benchmark results.

Reads the JSON/CSV records produced by :mod:`benchmarks.measurement`
(or accepts record dicts directly), aggregates them per
(provider, model), ranks candidates with a speed-focused score, and
emits a gateway-consumable fallback chain artifact in both YAML and
JSON.

Fully offline and deterministic — suitable for tests and CI.

Artifact format (``fallback_chain.yaml`` / ``.json``)
----------------------------------------------------

.. code-block:: yaml

    version: 1
    generated_at: "2026-08-16T00:00:00+00:00"
    source: results/run-abc.json        # where metrics came from
    scoring:
      weights: {tps: 0.5, ttft_inv: 0.3, success: 0.2}
      min_samples: 1
      min_success_rate: 0.0
    fallback_chain:                      # ranked, priority 1 = try first
      - provider: fast-gpt
        model: gpt-5-mini
        priority: 1
        score: 0.91
        metrics: {avg_tps: 120.5, avg_ttft_sec: 0.31, success_rate: 1.0, samples: 3}
    providers:                           # per-provider aggregate view
      - provider: fast-gpt
        rank: 1
        models: 2
        best_model: gpt-5-mini
        avg_tps: 120.5
        avg_ttft_sec: 0.31
        success_rate: 1.0

A gateway consumes ``fallback_chain``: on failure/timeout of priority N,
retry with N+1. The JSON file carries the identical structure.

CLI
---

.. code-block:: bash

    python -m benchmarks.fallback data/results.json -o data/fallback/
    python -m benchmarks.fallback data/results.json --max-entries 5 --min-samples 2
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a hard dep in requirements
    yaml = None

from .measurement import BenchmarkRecord, load_json

DEFAULT_WEIGHTS = {"tps": 0.5, "ttft_inv": 0.3, "success": 0.2}
DEFAULT_MIN_SAMPLES = 1
DEFAULT_MIN_SUCCESS_RATE = 0.0
ARTIFACT_VERSION = 1


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class CandidateMetrics:
    provider: str
    model: str
    avg_tps: float
    avg_ttft_sec: Optional[float]
    success_rate: float
    samples: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.model)


def aggregate(records: Iterable[BenchmarkRecord]) -> list[CandidateMetrics]:
    """Aggregate raw records into per-(provider, model) candidate metrics."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.provider is None or record.model is None:
            continue
        bucket = buckets.setdefault(
            (record.provider, record.model),
            {"tps": [], "ttft": [], "success": 0, "total": 0},
        )
        bucket["total"] += 1
        if record.status == "success":
            bucket["success"] += 1
            if record.tps is not None and record.tps > 0:
                bucket["tps"].append(record.tps)
            if record.ttft_sec is not None:
                bucket["ttft"].append(record.ttft_sec)

    candidates: list[CandidateMetrics] = []
    for (provider, model), b in buckets.items():
        candidates.append(
            CandidateMetrics(
                provider=provider,
                model=model,
                avg_tps=sum(b["tps"]) / len(b["tps"]) if b["tps"] else 0.0,
                avg_ttft_sec=(sum(b["ttft"]) / len(b["ttft"])) if b["ttft"] else None,
                success_rate=b["success"] / b["total"] if b["total"] else 0.0,
                samples=b["total"],
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _normalize(values: list[float]) -> list[float]:
    """Min-max normalize to [0, 1]; all-equal lists map to 0.5."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_candidates(
    candidates: list[CandidateMetrics],
    weights: Optional[dict[str, float]] = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
) -> list[dict]:
    """Filter, score and rank candidates (best first).

    Score = w_tps * norm(avg_tps) + w_ttft * (1 - norm(avg_ttft))
            + w_success * success_rate. TTFT is inverted so faster
    (smaller) is better; providers with no TTFT data get the neutral
    0.5 on that component. Ties break deterministically on
    (provider, model) so output is reproducible.
    """
    weights = dict(weights or DEFAULT_WEIGHTS)
    eligible = [
        c for c in candidates
        if c.samples >= min_samples and c.success_rate >= min_success_rate
    ]

    tps_n = _normalize([c.avg_tps for c in eligible])
    # Min-max normalize TTFT over candidates that HAVE TTFT data only.
    # Substituting 0.0 for missing entries would anchor the minimum of the
    # vector and compress every real TTFT's normalized score; candidates
    # without TTFT data keep the neutral 0.5 component instead.
    ttft_idx = [i for i, c in enumerate(eligible) if c.avg_ttft_sec is not None]
    ttft_n_map = dict(zip(
        ttft_idx, _normalize([eligible[i].avg_ttft_sec for i in ttft_idx])))

    scored: list[dict] = []
    for i, c in enumerate(eligible):
        ttft_component = 0.5 if c.avg_ttft_sec is None else (1.0 - ttft_n_map[i])
        score = (
            weights.get("tps", 0.0) * tps_n[i]
            + weights.get("ttft_inv", 0.0) * ttft_component
            + weights.get("success", 0.0) * c.success_rate
        )
        scored.append({
            "provider": c.provider,
            "model": c.model,
            "score": round(score, 6),
            "metrics": {
                "avg_tps": round(c.avg_tps, 3),
                "avg_ttft_sec": (round(c.avg_ttft_sec, 4) if c.avg_ttft_sec is not None else None),
                "success_rate": round(c.success_rate, 4),
                "samples": c.samples,
            },
        })

    scored.sort(key=lambda e: (-e["score"], e["provider"], e["model"]))
    for i, entry in enumerate(scored, start=1):
        entry["priority"] = i
    return scored


def _provider_summary(scored: list[dict]) -> list[dict]:
    """Collapse ranked entries into a per-provider ranking (best model wins)."""
    by_provider: dict[str, dict] = {}
    for entry in scored:
        summary = by_provider.setdefault(
            entry["provider"],
            {
                "provider": entry["provider"],
                "models": 0,
                "best_model": entry["model"],
                "avg_tps": entry["metrics"]["avg_tps"],
                "avg_ttft_sec": entry["metrics"]["avg_ttft_sec"],
                "success_rate": entry["metrics"]["success_rate"],
            },
        )
        summary["models"] += 1
    ranked = sorted(by_provider.values(), key=lambda p: (-p["avg_tps"], p["provider"]))
    for i, provider in enumerate(ranked, start=1):
        provider["rank"] = i
    return ranked


# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------

def build_fallback_config(
    records: Iterable[BenchmarkRecord],
    source: Optional[str] = None,
    weights: Optional[dict[str, float]] = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    max_entries: Optional[int] = None,
) -> dict:
    """Build the fallback chain config dict from raw benchmark records."""
    candidates = aggregate(records)
    scored = score_candidates(candidates, weights, min_samples, min_success_rate)
    if max_entries is not None:
        scored = scored[:max_entries]

    config: dict[str, Any] = {
        "version": ARTIFACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "scoring": {
            "weights": dict(weights or DEFAULT_WEIGHTS),
            "min_samples": min_samples,
            "min_success_rate": min_success_rate,
        },
        "fallback_chain": [
            {
                "provider": e["provider"],
                "model": e["model"],
                "priority": e["priority"],
                "score": e["score"],
                "metrics": e["metrics"],
            }
            for e in scored
        ],
        "providers": _provider_summary(scored),
    }
    return config


def write_fallback_config(
    config: dict,
    output_dir: str | Path,
    stem: str = "fallback_chain",
) -> list[Path]:
    """Write the config as both YAML and JSON. Returns the written paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{stem}.json"
    json_path.write_text(json.dumps(config, indent=2) + "\n")

    yaml_path = output_dir / f"{stem}.yaml"
    if yaml is not None:
        yaml_path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False))
    else:  # pragma: no cover
        raise RuntimeError("pyyaml is required to write the YAML artifact")

    return [yaml_path, json_path]


def generate_from_json(
    results_path: str | Path,
    output_dir: str | Path,
    **options: Any,
) -> list[Path]:
    """Convenience: load records from JSON and emit the fallback artifacts."""
    records = load_json(results_path)
    config = build_fallback_config(records, source=str(results_path), **options)
    return write_fallback_config(config, output_dir)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.fallback",
        description="Generate ranked fallback chain YAML/JSON from benchmark records.",
    )
    parser.add_argument("results", help="Path to benchmark results JSON (see benchmarks/measurement.py)")
    parser.add_argument("-o", "--output-dir", default="data/fallback", help="Output directory")
    parser.add_argument("--max-entries", type=int, default=None, help="Cap chain length")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--min-success-rate", type=float, default=DEFAULT_MIN_SUCCESS_RATE)
    parser.add_argument("--weight-tps", type=float, default=DEFAULT_WEIGHTS["tps"])
    parser.add_argument("--weight-ttft", type=float, default=DEFAULT_WEIGHTS["ttft_inv"])
    parser.add_argument("--weight-success", type=float, default=DEFAULT_WEIGHTS["success"])
    args = parser.parse_args(argv)

    weights = {"tps": args.weight_tps, "ttft_inv": args.weight_ttft, "success": args.weight_success}
    paths = generate_from_json(
        args.results,
        args.output_dir,
        weights=weights,
        min_samples=args.min_samples,
        min_success_rate=args.min_success_rate,
        max_entries=args.max_entries,
    )
    chain = json.loads(paths[1].read_text())["fallback_chain"]
    print(f"Wrote {len(paths)} artifacts:")
    for p in paths:
        print(f"  {p}")
    print(f"\nFallback chain ({len(chain)} entries):")
    for e in chain:
        m = e["metrics"]
        ttft = f"{m['avg_ttft_sec']:.3f}s" if m["avg_ttft_sec"] is not None else "-"
        print(
            f"  #{e['priority']:2} {e['provider']:16} {e['model']:40} "
            f"score={e['score']:.3f} tps={m['avg_tps']:8.1f} ttft={ttft:>8} ok={m['success_rate']:.0%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
