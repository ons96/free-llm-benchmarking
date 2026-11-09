"""Tests for freeradar.rank module."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.rank import (
    rank_models,
    rank_all_tasks,
    _normalize_model_name,
    _get_quality_score,
    _compute_speed_score,
    _compute_reliability_score,
    _compute_task_score,
    TASK_WEIGHTS,
    RankedModel,
)
from freeradar.registry import init_db, upsert_model, connect, HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_UNHEALTHY, HEALTH_UNKNOWN


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    # Add some test models
    conn = connect(db_path)
    upsert_model(conn, "provider1", "model-a", {"context": 32768, "modalities": ["text"], "tool_calls": True, "quality_score": 0.9})
    upsert_model(conn, "provider1", "model-b", {"context": 8192, "modalities": ["text"], "tool_calls": True, "quality_score": 0.7})
    upsert_model(conn, "provider2", "model-c", {"context": 131072, "modalities": ["text", "vision"], "tool_calls": False, "quality_score": 0.8})
    upsert_model(conn, "provider2", "model-d", {"context": 4096, "modalities": ["text"], "tool_calls": True, "quality_score": 0.6})
    upsert_model(conn, "provider3", "model-e", {"context": 16384, "modalities": ["text"], "tool_calls": True, "quality_score": None})
    conn.commit()
    conn.close()
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def temp_db_with_probes(temp_db):
    conn = connect(temp_db)
    # Apply probe outcomes for some models
    from freeradar.registry import apply_probe_outcome
    apply_probe_outcome(conn, "provider1", "model-a", True, 100.0, 50, 500.0, 200)
    apply_probe_outcome(conn, "provider1", "model-a", True, 120.0, 60, 500.0, 200)
    apply_probe_outcome(conn, "provider1", "model-a", True, 90.0, 40, 444.0, 200)  # 3 healthy streak

    apply_probe_outcome(conn, "provider1", "model-b", True, 200.0, 30, 150.0, 200)
    apply_probe_outcome(conn, "provider1", "model-b", False, None, None, None, 429)  # streak broken

    apply_probe_outcome(conn, "provider2", "model-c", True, 50.0, 100, 2000.0, 200)
    apply_probe_outcome(conn, "provider2", "model-c", True, 60.0, 120, 2000.0, 200)
    apply_probe_outcome(conn, "provider2", "model-c", True, 55.0, 110, 2000.0, 200)
    apply_probe_outcome(conn, "provider2", "model-c", True, 65.0, 130, 2000.0, 200)
    apply_probe_outcome(conn, "provider2", "model-c", True, 70.0, 140, 2000.0, 200)  # 5 healthy streak

    apply_probe_outcome(conn, "provider2", "model-d", False, None, None, None, 500)
    apply_probe_outcome(conn, "provider3", "model-e", True, 300.0, 20, 66.0, 200)
    conn.commit()
    conn.close()
    yield temp_db


def test_normalize_model_name():
    assert _normalize_model_name("openai/gpt-4") == "gpt-4"
    assert _normalize_model_name("gpt-4-20250101") == "gpt-4"
    assert _normalize_model_name("gpt-4-2025-01-01") == "gpt-4"
    assert _normalize_model_name("gpt-4-preview") == "gpt-4"
    assert _normalize_model_name("gpt-4-latest") == "gpt-4"
    assert _normalize_model_name("model:free") == "model"
    assert _normalize_model_name("model:online") == "model"
    assert _normalize_model_name("model-instruct") == "model"
    assert _normalize_model_name("gpt4") == "gpt-4"
    assert _normalize_model_name("DeepSeek-V3") == "deepseek-v3"


def test_get_quality_score():
    benchmarks = {"gpt-4": 0.95, "deepseek-v3": 0.88, "llama-3-70b": 0.82}
    assert _get_quality_score("gpt-4", benchmarks) == 0.95
    assert _get_quality_score("openai/gpt-4", benchmarks) == 0.95
    assert _get_quality_score("gpt-4-20250101", benchmarks) == 0.95
    assert _get_quality_score("unknown-model", benchmarks) == 0.0


def test_compute_speed_score():
    assert _compute_speed_score(None, None) == 0.5
    assert _compute_speed_score(100.0, None) == 1.0  # 100ms -> 1.0
    assert _compute_speed_score(500.0, None) == 0.2  # 500ms -> 0.2
    assert _compute_speed_score(None, 100.0) == 1.0  # 100 tps -> 1.0
    assert _compute_speed_score(None, 50.0) == 0.5  # 50 tps -> 0.5
    # Both provided - average
    score = _compute_speed_score(100.0, 100.0)
    assert 0.5 <= score <= 1.0


def test_compute_reliability_score():
    # Healthy with good streak
    score = _compute_reliability_score(HEALTH_HEALTHY, 5, 10, 10)
    assert score > 0.8

    # Degraded
    score = _compute_reliability_score(HEALTH_DEGRADED, 0, 10, 5)
    assert 0.3 < score < 0.6

    # Unhealthy
    score = _compute_reliability_score(HEALTH_UNHEALTHY, 0, 10, 2)
    assert score < 0.3

    # Unknown
    score = _compute_reliability_score(HEALTH_UNKNOWN, 0, 0, 0)
    assert score < 0.3


def test_compute_task_score(temp_db_with_probes):
    conn = connect(temp_db_with_probes)
    from freeradar.registry import get_model
    model = get_model(conn, "provider1", "model-a")
    conn.close()

    # Test coding task (high quality weight)
    composite, quality, speed, reliability = _compute_task_score(model, "coding", {})
    assert composite > 0
    assert quality == 0.9  # from quality_score field
    assert 0 <= speed <= 1
    assert 0 <= reliability <= 1


def test_rank_models_coding(temp_db_with_probes):
    ranked = rank_models("coding", top_n=5, db_path=temp_db_with_probes)
    assert len(ranked) <= 5
    # model-a should rank high (high quality, tool_calls, good streak)
    assert ranked[0].model_id == "model-a"
    assert ranked[0].provider == "provider1"
    assert ranked[0].task_class == "coding"
    assert ranked[0].tool_calls is True


def test_rank_models_reasoning(temp_db_with_probes):
    ranked = rank_models("reasoning", top_n=5, db_path=temp_db_with_probes)
    assert len(ranked) <= 5
    # model-c has vision but no tool_calls, might be excluded for reasoning
    # model-a has tool_calls and high quality
    assert ranked[0].tool_calls is True


def test_rank_models_long_context(temp_db_with_probes):
    ranked = rank_models("long-context", top_n=5, db_path=temp_db_with_probes)
    assert len(ranked) <= 5
    # Should prefer models with context >= 16384
    for r in ranked:
        assert r.context >= 16384


def test_rank_models_vision(temp_db_with_probes):
    ranked = rank_models("vision", top_n=5, db_path=temp_db_with_probes)
    # model-c has vision modality
    assert any(r.model_id == "model-c" for r in ranked)


def test_rank_models_bulk(temp_db_with_probes):
    ranked = rank_models("bulk", top_n=5, db_path=temp_db_with_probes)
    assert len(ranked) <= 5


def test_rank_models_fast(temp_db_with_probes):
    ranked = rank_models("fast", top_n=5, db_path=temp_db_with_probes)
    assert len(ranked) <= 5
    # Fast task weights speed heavily


def test_rank_all_tasks(temp_db_with_probes):
    all_ranked = rank_all_tasks(top_n=3, db_path=temp_db_with_probes)
    assert set(all_ranked.keys()) == set(TASK_WEIGHTS.keys())
    for task, ranked in all_ranked.items():
        assert len(ranked) <= 3


def test_rank_models_empty_db():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    try:
        ranked = rank_models("coding", top_n=10, db_path=db_path)
        assert ranked == []
    finally:
        db_path.unlink(missing_ok=True)


def test_rank_models_invalid_task():
    with pytest.raises(ValueError):
        rank_models("invalid-task", db_path=Path(":memory:"))


def test_task_weights_sum_to_one():
    for task, weights in TASK_WEIGHTS.items():
        q, s, r = weights
        assert abs(q + s + r - 1.0) < 0.001


def test_ranked_model_dataclass():
    rm = RankedModel(
        provider="p1",
        model_id="m1",
        task_class="coding",
        score=0.85,
        quality=0.9,
        speed=0.8,
        reliability=0.85,
        context=32768,
        modalities=["text"],
        tool_calls=True,
        p50_latency=100.0,
        throughput=500.0,
        quality_score=0.9,
        last_verified="2025-01-01T00:00:00+00:00",
        health=HEALTH_HEALTHY,
        source_url="https://example.com",
        consecutive_healthy=3,
        total_probes=5,
        success_count=4,
    )
    assert rm.provider == "p1"
    assert rm.score == 0.85


if __name__ == "__main__":
    pytest.main([__file__, "-v"])