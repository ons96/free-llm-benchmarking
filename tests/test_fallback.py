"""Offline tests for fallback chain generation (benchmarks/fallback.py)."""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.fallback import (
    aggregate,
    build_fallback_config,
    generate_from_json,
    score_candidates,
    write_fallback_config,
)
from benchmarks.measurement import BenchmarkRecord, write_json


def rec(provider, model, tps=None, ttft=None, status="success", tokens=100):
    return BenchmarkRecord(
        provider=provider, model=model, status=status,
        tps=tps, ttft_sec=ttft, output_tokens=tokens if status == "success" else 0,
        total_time_sec=1.0,
    )


class TestAggregation:
    def test_per_provider_model_bucketing(self):
        records = [
            rec("a", "m1", tps=10, ttft=0.5),
            rec("a", "m1", tps=30, ttft=0.7),
            rec("b", "m2", tps=20, ttft=0.1),
        ]
        cands = {(c.provider, c.model): c for c in aggregate(records)}
        assert cands[("a", "m1")].avg_tps == 20.0
        assert cands[("a", "m1")].samples == 2
        assert cands[("b", "m2")].avg_tps == 20.0

    def test_failures_count_against_success_rate_not_tps(self):
        records = [
            rec("a", "m1", tps=50, ttft=0.2),
            rec("a", "m1", status="timeout"),
        ]
        cands = {(c.provider, c.model): c for c in aggregate(records)}
        c = cands[("a", "m1")]
        assert c.success_rate == 0.5
        assert c.avg_tps == 50.0
        assert c.samples == 2


class TestScoring:
    def test_fast_provider_ranks_first(self):
        records = [
            rec("fast", "m", tps=200, ttft=0.1),
            rec("slow", "m", tps=10, ttft=2.0),
        ]
        scored = score_candidates(aggregate(records))
        assert scored[0]["provider"] == "fast"
        assert scored[0]["priority"] == 1
        assert scored[0]["score"] > scored[1]["score"]

    def test_low_ttft_beats_high_ttft_at_equal_tps(self):
        records = [
            rec("snappy", "m", tps=50, ttft=0.05),
            rec("laggy", "m", tps=50, ttft=3.0),
        ]
        scored = score_candidates(aggregate(records))
        assert scored[0]["provider"] == "snappy"

    def test_min_samples_filters_thin_results(self):
        records = [
            rec("thick", "m", tps=10),
            rec("thick", "m", tps=12),
            rec("thin", "m", tps=1000),
        ]
        scored = score_candidates(aggregate(records), min_samples=2)
        providers = {e["provider"] for e in scored}
        assert "thin" not in providers

    def test_min_success_rate_filters_flaky_providers(self):
        records = [rec("flaky", "m", tps=100)] + [rec("flaky", "m", status="error")] * 3
        scored = score_candidates(aggregate(records), min_success_rate=0.5)
        assert scored == []

    def test_tie_break_is_deterministic(self):
        records = [rec("b", "x", tps=50, ttft=0.5), rec("a", "y", tps=50, ttft=0.5)]
        s1 = score_candidates(aggregate(records))
        s2 = score_candidates(aggregate(list(reversed(records))))
        assert [(e["provider"], e["priority"]) for e in s1] == [(e["provider"], e["priority"]) for e in s2]


class TestFallbackConfig:
    def test_artifact_structure(self):
        records = [
            rec("fast", "m1", tps=200, ttft=0.1),
            rec("slow", "m2", tps=20, ttft=1.0),
            rec("slow", "m2", status="error"),
        ]
        config = build_fallback_config(records, source="test-run")
        assert config["version"] == 1
        assert config["source"] == "test-run"
        assert "generated_at" in config

        chain = config["fallback_chain"]
        assert [e["priority"] for e in chain] == list(range(1, len(chain) + 1))
        assert chain[0]["provider"] == "fast"
        assert chain[0]["metrics"]["samples"] == 1
        assert chain[1]["metrics"]["success_rate"] == 0.5

        providers = config["providers"]
        assert providers[0]["provider"] == "fast"
        assert providers[0]["rank"] == 1
        assert "weights" in config["scoring"]

    def test_max_entries_caps_chain(self):
        records = [rec(f"p{i}", "m", tps=i) for i in range(10)]
        config = build_fallback_config(records, max_entries=3)
        assert len(config["fallback_chain"]) == 3

    def test_yaml_and_json_artifacts_written(self, tmp_path):
        records = [rec("fast", "m1", tps=200, ttft=0.1), rec("slow", "m2", tps=20, ttft=1.0)]
        config = build_fallback_config(records, source="unit")
        paths = write_fallback_config(config, tmp_path / "out")

        yaml_path = tmp_path / "out" / "fallback_chain.yaml"
        json_path = tmp_path / "out" / "fallback_chain.json"
        assert set(paths) == {yaml_path, json_path}

        from_yaml = yaml.safe_load(yaml_path.read_text())
        from_json = json.loads(json_path.read_text())
        assert from_yaml["fallback_chain"] == from_json["fallback_chain"]
        assert from_json["fallback_chain"][0]["provider"] == "fast"

    def test_generate_from_json_end_to_end(self, tmp_path):
        records = [
            rec("prov-a", "model-1", tps=120, ttft=0.3),
            rec("prov-b", "model-2", tps=40, ttft=0.8),
        ]
        results_path = write_json(records, tmp_path / "results.json")
        paths = generate_from_json(results_path, tmp_path / "fallback")
        assert len(paths) == 2

        config = json.loads((tmp_path / "fallback" / "fallback_chain.json").read_text())
        assert config["source"] == str(results_path)
        assert config["fallback_chain"][0]["provider"] == "prov-a"
