"""Tests for freeradar.aliases module."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.aliases import (
    compute_alias_proposals,
    apply_alias_proposals,
    get_alias_status,
    MIN_CONSECUTIVE_HEALTHY,
    AliasProposal,
)
from freeradar.registry import init_db, upsert_model, connect, set_alias, HEALTH_HEALTHY, ALIASES
from freeradar.rank import rank_models


@pytest.fixture
def temp_db_with_data():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    conn = connect(db_path)
    # Add models with different health/streak states
    upsert_model(conn, "p1", "model-a", {"context": 32768, "tool_calls": True, "quality_score": 0.9})
    upsert_model(conn, "p1", "model-b", {"context": 8192, "tool_calls": True, "quality_score": 0.7})
    upsert_model(conn, "p2", "model-c", {"context": 131072, "tool_calls": True, "quality_score": 0.8})
    upsert_model(conn, "p2", "model-d", {"context": 4096, "tool_calls": True, "quality_score": 0.6})

    # Apply probe outcomes to set streaks
    from freeradar.registry import apply_probe_outcome
    # model-a: 3 healthy (meets MIN_CONSECUTIVE_HEALTHY=3)
    for _ in range(3):
        apply_probe_outcome(conn, "p1", "model-a", True, 100.0, 50, 500.0, 200)
    # model-b: 1 healthy then fail
    apply_probe_outcome(conn, "p1", "model-b", True, 200.0, 30, 150.0, 200)
    apply_probe_outcome(conn, "p1", "model-b", False, None, None, None, 429)
    # model-c: 5 healthy
    for _ in range(5):
        apply_probe_outcome(conn, "p2", "model-c", True, 50.0, 100, 2000.0, 200)
    # model-d: unhealthy
    apply_probe_outcome(conn, "p2", "model-d", False, None, None, None, 500)

    conn.commit()
    conn.close()
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def temp_db_empty():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    yield db_path
    db_path.unlink(missing_ok=True)


def test_compute_alias_proposals_basic(temp_db_with_data):
    proposals = compute_alias_proposals(temp_db_with_data, min_streak=3)

    # Should have proposals for all aliases
    assert len(proposals) == len(ALIASES)

    # Find coding proposal
    coding_prop = next(p for p in proposals if p.alias == "free-best-coding")
    assert coding_prop.task == "coding"
    # Best model for coding is model-a (3 streak, high quality, tool_calls)
    assert coding_prop.proposed_provider == "p1"
    assert coding_prop.proposed_model == "model-a"
    assert coding_prop.action in ("promote", "init")
    assert coding_prop.healthy_streak == 3


def test_compute_alias_proposals_fast_task(temp_db_with_data):
    proposals = compute_alias_proposals(temp_db_with_data, min_streak=3)

    fast_prop = next(p for p in proposals if p.alias == "free-best-fast")
    assert fast_prop.task == "fast"
    # model-c is fastest (low latency, high throughput, 5 streak)
    assert fast_prop.proposed_provider == "p2"
    assert fast_prop.proposed_model == "model-c"


def test_compute_alias_proposals_no_eligible(temp_db_empty):
    proposals = compute_alias_proposals(temp_db_empty, min_streak=3)

    for p in proposals:
        assert p.action == "keep"
        assert p.proposed_provider is None
        assert "no eligible models" in p.reason or "streak requirement" in p.reason


def test_compute_alias_proposals_streak_not_met(temp_db_with_data):
    # Require higher streak
    proposals = compute_alias_proposals(temp_db_with_data, min_streak=5)

    coding_prop = next(p for p in proposals if p.alias == "free-best-coding")
    # model-a has streak=3, doesn't meet 5
    # model-c has streak=5 but may not be best for coding
    assert coding_prop.action == "keep"
    assert "waiting for streak" in coding_prop.reason or "no model meets" in coding_prop.reason


def test_apply_alias_proposals_dry_run(temp_db_with_data):
    proposals = compute_alias_proposals(temp_db_with_data, min_streak=3)
    result = apply_alias_proposals(proposals, temp_db_with_data, dry_run=True)

    assert result["applied"] is False
    assert "changes" in result
    assert result["backup"] is None
    assert len(result["changes"]) > 0

    # Check change structure
    ch = result["changes"][0]
    assert "alias" in ch
    assert "action" in ch
    assert "from" in ch
    assert "to" in ch
    assert "score" in ch
    assert "reason" in ch


def test_apply_alias_proposals_apply(temp_db_with_data):
    proposals = compute_alias_proposals(temp_db_with_data, min_streak=3)
    result = apply_alias_proposals(proposals, temp_db_with_data, dry_run=False, backup=False)

    assert result["applied"] is True
    assert len(result["changes"]) > 0

    # Verify registry updated
    conn = connect(temp_db_with_data)
    from freeradar.registry import get_alias
    alias = get_alias(conn, "free-best-coding")
    conn.close()
    assert alias is not None
    assert alias["provider"] == "p1"
    assert alias["model_id"] == "model-a"


def test_apply_alias_proposals_creates_backup(temp_db_with_data):
    import shutil
    from freeradar.aliases import GATEWAY_VIRTUAL_PATH

    # Create dummy gateway yaml
    GATEWAY_VIRTUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    GATEWAY_VIRTUAL_PATH.write_text("virtual_models: {}\n")

    try:
        proposals = compute_alias_proposals(temp_db_with_data, min_streak=3)
        result = apply_alias_proposals(proposals, temp_db_with_data, dry_run=False, backup=True)

        assert result["backup"] is not None
        backup_path = Path(result["backup"])
        assert backup_path.exists()
        # Path.suffix only returns the last extension, so check the name instead.
        assert backup_path.name.endswith(".yaml.bak")
    finally:
        if GATEWAY_VIRTUAL_PATH.exists():
            GATEWAY_VIRTUAL_PATH.unlink()
        for bak in GATEWAY_VIRTUAL_PATH.parent.glob("*.bak*"):
            bak.unlink(missing_ok=True)


def test_get_alias_status(temp_db_with_data):
    conn = connect(temp_db_with_data)
    set_alias(conn, "free-best-coding", "coding", "p1", "model-a", 0.85, 3, "p2", "model-c")
    conn.commit()
    conn.close()

    status = get_alias_status(temp_db_with_data)
    assert "free-best-coding" in status
    info = status["free-best-coding"]
    assert info["task"] == "coding"
    assert info["provider"] == "p1"
    assert info["model"] == "model-a"
    assert info["score"] == 0.85
    assert info["healthy_streak"] == 3
    assert info["fallback_provider"] == "p2"
    assert info["fallback_model"] == "model-c"


def test_alias_proposal_dataclass():
    prop = AliasProposal(
        alias="free-best-coding",
        task="coding",
        current_provider="p1",
        current_model="model-a",
        proposed_provider="p2",
        proposed_model="model-b",
        score=0.9,
        healthy_streak=5,
        fallback_provider="p1",
        fallback_model="model-a",
        action="promote",
        reason="new best model",
    )
    assert prop.alias == "free-best-coding"
    assert prop.action == "promote"
    assert prop.score == 0.9


def test_min_consecutive_healthy_constant():
    assert MIN_CONSECUTIVE_HEALTHY == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])