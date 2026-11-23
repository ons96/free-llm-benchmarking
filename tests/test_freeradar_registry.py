"""Tests for freeradar.registry module."""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.registry import (
    connect,
    init_db,
    now_iso,
    get_model,
    list_models,
    upsert_model,
    mark_missing,
    record_probe,
    apply_probe_outcome,
    set_provider_health,
    get_provider_health,
    in_cooldown,
    get_alias,
    list_aliases,
    set_alias,
    alias_history,
    log_discovery,
    stale_entries,
    top_movers,
    stability_report,
    export_json,
    HEALTH_HEALTHY,
    HEALTH_DEGRADED,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
    TASK_CLASSES,
    ALIASES,
    DB_PATH,
    JSON_PATH,
    DISCOVERY_FIELDS,
)


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def conn(temp_db):
    c = connect(temp_db)
    yield c
    c.close()


def test_init_db_creates_tables(temp_db):
    c = connect(temp_db)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    c.close()
    assert "models" in tables
    assert "probe_history" in tables
    assert "aliases" in tables
    assert "alias_history" in tables
    assert "provider_health" in tables
    assert "discovery_log" in tables


def test_now_iso_format():
    iso = now_iso()
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo == timezone.utc


def test_upsert_model_adds_new(conn):
    result = upsert_model(conn, "test-provider", "test-model", {"context": 8192, "tool_calls": True})
    assert result == "added"

    model = get_model(conn, "test-provider", "test-model")
    assert model is not None
    assert model["provider"] == "test-provider"
    assert model["model_id"] == "test-model"
    assert model["context"] == 8192
    assert model["tool_calls"] is True
    assert model["health"] == HEALTH_UNKNOWN
    assert model["status"] == "active"
    assert model["free_tier"] is True


def test_upsert_model_updates_existing(conn):
    upsert_model(conn, "p1", "m1", {"context": 4096})
    result = upsert_model(conn, "p1", "m1", {"context": 8192})
    assert result == "updated"

    model = get_model(conn, "p1", "m1")
    assert model["context"] == 8192


def test_upsert_model_unchanged(conn):
    upsert_model(conn, "p1", "m1", {"context": 4096})
    result = upsert_model(conn, "p1", "m1", {"context": 4096})
    assert result == "unchanged"


def test_upsert_model_ignores_non_discovery_fields(conn):
    # health, streak, etc. should not be updatable via discovery
    upsert_model(conn, "p1", "m1", {"context": 4096})
    # Manually set health
    conn.execute("UPDATE models SET health = ? WHERE provider = ? AND model_id = ?", (HEALTH_HEALTHY, "p1", "m1"))
    conn.commit()

    # Try to overwrite via discovery fields (should not affect health)
    upsert_model(conn, "p1", "m1", {"context": 8192})

    model = get_model(conn, "p1", "m1")
    assert model["context"] == 8192
    assert model["health"] == HEALTH_HEALTHY  # Preserved


def test_list_models_filters(conn):
    upsert_model(conn, "p1", "m1")
    upsert_model(conn, "p1", "m2")
    upsert_model(conn, "p2", "m1")
    # Set free_tier via SQL (not in DISCOVERY_FIELDS)
    conn.execute("UPDATE models SET free_tier = 1 WHERE provider = 'p1' AND model_id = 'm1'")
    conn.execute("UPDATE models SET free_tier = 0 WHERE provider = 'p1' AND model_id = 'm2'")
    conn.execute("UPDATE models SET free_tier = 1 WHERE provider = 'p2' AND model_id = 'm1'")
    # Mark one as removed
    conn.execute("UPDATE models SET status = 'removed' WHERE provider = 'p1' AND model_id = 'm1'")
    conn.commit()

    active_free = list_models(conn, active_only=True, free_only=True)
    assert len(active_free) == 1
    assert active_free[0]["provider"] == "p2"

    all_models = list_models(conn, active_only=False, free_only=False)
    assert len(all_models) == 3


def test_mark_missing(conn):
    upsert_model(conn, "p1", "m1")
    upsert_model(conn, "p1", "m2")
    upsert_model(conn, "p2", "m1")

    removed = mark_missing(conn, [("p1", "m1")])
    assert removed == 2

    m1 = get_model(conn, "p1", "m1")
    m2 = get_model(conn, "p1", "m2")
    assert m1["status"] == "active"
    assert m2["status"] == "removed"


def test_record_probe_and_apply_outcome(conn):
    record_probe(conn, "p1", "m1", "chat", True, 200, 100.0, 50, 500.0, None, None)
    record_probe(conn, "p1", "m1", "chat", False, 429, 200.0, 0, 0.0, "rate limited", 60.0)

    probes = conn.execute("SELECT * FROM probe_history WHERE provider = ? AND model_id = ?", ("p1", "m1")).fetchall()
    assert len(probes) == 2
    assert probes[0]["ok"] == 1
    assert probes[1]["ok"] == 0
    assert probes[1]["retry_after"] == 60.0

    # Apply outcome - first call creates the model and sets total_probes=1
    result = apply_probe_outcome(conn, "p1", "m1", True, 100.0, 50, 500.0, 200)
    assert result["health"] == HEALTH_HEALTHY
    assert result["consecutive_healthy"] == 1
    assert result["total_probes"] == 1  # First apply_probe_outcome call
    assert result["success_count"] == 1
    assert result["p50_latency"] == 100.0
    assert result["throughput"] == 500.0

    # Second outcome - failure
    result2 = apply_probe_outcome(conn, "p1", "m1", False, None, None, None, 429)
    assert result2["health"] == HEALTH_DEGRADED
    assert result2["consecutive_healthy"] == 0
    assert result2["total_probes"] == 2


def test_apply_probe_outcome_health_transitions(conn):
    upsert_model(conn, "p1", "m1")

    # 200 -> healthy
    apply_probe_outcome(conn, "p1", "m1", True, 100.0, 10, 100.0, 200)
    m = get_model(conn, "p1", "m1")
    assert m["health"] == HEALTH_HEALTHY

    # 401 -> unhealthy
    apply_probe_outcome(conn, "p1", "m1", False, None, None, None, 401)
    m = get_model(conn, "p1", "m1")
    assert m["health"] == HEALTH_UNHEALTHY

    # 429 -> degraded
    apply_probe_outcome(conn, "p1", "m1", False, None, None, None, 429)
    m = get_model(conn, "p1", "m1")
    assert m["health"] == HEALTH_DEGRADED

    # 500 -> degraded
    apply_probe_outcome(conn, "p1", "m1", False, None, None, None, 500)
    m = get_model(conn, "p1", "m1")
    assert m["health"] == HEALTH_DEGRADED


def test_provider_health(conn):
    set_provider_health(conn, "p1", HEALTH_HEALTHY, free_quota="unlimited", rate_limit="30")
    ph = get_provider_health(conn, "p1")
    assert ph["health"] == HEALTH_HEALTHY
    assert ph["free_quota"] == "unlimited"
    assert ph["rate_limit"] == "30"

    # Update
    set_provider_health(conn, "p1", HEALTH_DEGRADED, last_error="rate limited")
    ph = get_provider_health(conn, "p1")
    assert ph["health"] == HEALTH_DEGRADED
    assert ph["last_error"] == "rate limited"
    assert ph["free_quota"] == "unlimited"  # Preserved


def test_in_cooldown(conn):
    # No cooldown initially
    assert not in_cooldown(conn, "p1")

    # Set cooldown in future (1 hour from now)
    future = (datetime.now(timezone.utc) + __import__('datetime').timedelta(hours=1)).isoformat()
    set_provider_health(conn, "p1", HEALTH_DEGRADED, cooldown_until=future)
    assert in_cooldown(conn, "p1")

    # Set cooldown in past
    past = (datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=2)).isoformat()
    set_provider_health(conn, "p2", HEALTH_DEGRADED, cooldown_until=past)
    assert not in_cooldown(conn, "p2")


def test_alias_management(conn):
    # Set alias
    set_alias(conn, "free-best-coding", "coding", "p1", "m1", 0.85, 3, "p2", "m2", "promote")
    alias = get_alias(conn, "free-best-coding")
    assert alias["provider"] == "p1"
    assert alias["model_id"] == "m1"
    assert alias["score"] == 0.85
    assert alias["healthy_streak"] == 3
    assert alias["fallback_provider"] == "p2"
    assert alias["fallback_model_id"] == "m2"

    # Update same alias (no provider/model change)
    set_alias(conn, "free-best-coding", "coding", "p1", "m1", 0.90, 4, "p2", "m2", "promote")
    alias = get_alias(conn, "free-best-coding")
    assert alias["score"] == 0.90
    assert alias["healthy_streak"] == 4

    # Change alias target - should create history
    set_alias(conn, "free-best-coding", "coding", "p3", "m3", 0.95, 5, "p1", "m1", "promote")
    alias = get_alias(conn, "free-best-coding")
    assert alias["provider"] == "p3"
    assert alias["model_id"] == "m3"
    assert alias["fallback_provider"] == "p1"

    history = alias_history(conn, "free-best-coding")
    assert len(history) == 2  # Initial + change
    assert history[0]["action"] == "promote"
    assert history[0]["previous_provider"] is None  # First insert
    assert history[1]["action"] == "promote"
    assert history[1]["previous_provider"] == "p1"  # Old provider before change


def test_log_discovery(conn):
    log_discovery(conn, "opencode", "p1", "m1", "added")
    log_discovery(conn, "openrouter", "p2", "m2", "added")

    rows = conn.execute("SELECT * FROM discovery_log ORDER BY ts").fetchall()
    assert len(rows) == 2
    assert rows[0]["source"] == "opencode"
    assert rows[0]["action"] == "added"
    assert rows[1]["source"] == "openrouter"


def test_stale_entries(conn):
    old = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=10)).isoformat()
    recent = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=1)).isoformat()

    upsert_model(conn, "p1", "m1")
    upsert_model(conn, "p1", "m2")
    conn.execute("UPDATE models SET last_verified = ? WHERE provider = ? AND model_id = ?", (old, "p1", "m1"))
    conn.execute("UPDATE models SET last_verified = ? WHERE provider = ? AND model_id = ?", (recent, "p1", "m2"))
    conn.commit()

    stale = stale_entries(conn, days=7)
    assert len(stale) == 1
    assert stale[0]["model_id"] == "m1"

    never = stale_entries(conn, days=7)
    # m2 is not stale (verified recently)


def test_top_movers(conn):
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=10)).isoformat()

    record_probe(conn, "p1", "m1", "chat", True, 200, 100.0, 10, 100.0, None, None, now)
    record_probe(conn, "p1", "m1", "chat", True, 200, 100.0, 10, 100.0, None, None, now)
    record_probe(conn, "p2", "m1", "chat", False, 429, 200.0, 0, 0.0, "rate limited", 60.0, now)
    record_probe(conn, "p3", "m1", "chat", True, 200, 100.0, 10, 100.0, None, None, old)  # Outside window

    movers = top_movers(conn, days=7)
    assert len(movers) == 2  # p3 excluded (old)
    assert movers[0]["provider"] == "p1"
    assert movers[0]["success_rate"] == 1.0


def test_stability_report(conn):
    set_alias(conn, "free-best-coding", "coding", "p1", "m1", 0.85, 5, "p2", "m2")
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(5):
        record_probe(conn, "p1", "m1", "chat", True, 200, 100.0, 10, 100.0, None, None, now)

    report = stability_report(conn, days=7)
    assert report["window_days"] == 7
    assert len(report["aliases"]) == 1
    alias = report["aliases"][0]
    assert alias["alias"] == "free-best-coding"
    assert alias["probes"] == 5
    assert alias["success_rate"] == 1.0
    assert alias["alias_changes"] == 0
    assert alias["stable"] is True


def test_export_json(conn, temp_db):
    upsert_model(conn, "p1", "m1", {"context": 8192, "tool_calls": True})
    set_alias(conn, "free-best-coding", "coding", "p1", "m1", 0.85, 3, "p2", "m2")
    set_provider_health(conn, "p1", HEALTH_HEALTHY, free_quota="unlimited")

    json_file = temp_db.parent / "test_export.json"
    export_json(conn, json_file, temp_db)

    with open(json_file) as f:
        data = json.load(f)

    assert data["version"] == 1
    assert len(data["models"]) == 1
    assert data["models"][0]["provider"] == "p1"
    assert len(data["aliases"]) == 1
    assert len(data["provider_health"]) == 1
    json_file.unlink()


def test_percentile():
    from freeradar.registry import percentile
    assert percentile([100], 50) == 100
    assert percentile([100, 200], 50) == 150
    assert percentile([100, 200, 300], 50) == 200
    assert percentile([100, 200, 300, 400], 50) == 250
    assert percentile([], 50) == 0.0


def test_task_classes_and_aliases():
    assert TASK_CLASSES == ("coding", "reasoning", "long-context", "vision", "bulk")
    assert ALIASES["free-best-coding"] == "coding"
    assert ALIASES["free-best-reasoning"] == "reasoning"
    assert ALIASES["free-best-long"] == "long-context"
    assert ALIASES["free-best-vision"] == "vision"
    assert ALIASES["free-best-bulk"] == "bulk"
    assert ALIASES["free-best-fast"] == "fast"


def test_discovery_fields_defined():
    expected = {"context", "modalities", "tool_calls", "free_quota", "rate_limit", "reset_window", "quality_score", "source_url", "tos_notes"}
    assert set(DISCOVERY_FIELDS) == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])