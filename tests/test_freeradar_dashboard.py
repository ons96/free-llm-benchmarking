"""Tests for freeradar.dashboard module."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.dashboard import (
    generate_dashboard,
    generate_html_dashboard,
)
from freeradar.registry import init_db, upsert_model, connect, set_alias, set_provider_health, HEALTH_HEALTHY, apply_probe_outcome, record_probe
from freeradar.aliases import compute_alias_proposals, apply_alias_proposals


@pytest.fixture
def temp_db_with_data():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    conn = connect(db_path)

    # Add models
    upsert_model(conn, "p1", "model-a", {"context": 32768, "tool_calls": True, "quality_score": 0.9})
    upsert_model(conn, "p1", "model-b", {"context": 8192, "tool_calls": True, "quality_score": 0.7})
    upsert_model(conn, "p2", "model-c", {"context": 131072, "tool_calls": True, "quality_score": 0.8})

    # Probe outcomes
    apply_probe_outcome(conn, "p1", "model-a", True, 100.0, 50, 500.0, 200)
    apply_probe_outcome(conn, "p1", "model-a", True, 120.0, 60, 500.0, 200)
    apply_probe_outcome(conn, "p1", "model-a", True, 90.0, 40, 444.0, 200)

    apply_probe_outcome(conn, "p2", "model-c", True, 50.0, 100, 2000.0, 200)
    apply_probe_outcome(conn, "p2", "model-c", True, 60.0, 120, 2000.0, 200)

    # Alias
    set_alias(conn, "free-best-coding", "coding", "p1", "model-a", 0.85, 3, "p2", "model-c")

    # Provider health
    set_provider_health(conn, "p1", HEALTH_HEALTHY, free_quota="unlimited", rate_limit="30")

    conn.commit()
    conn.close()
    yield db_path
    db_path.unlink(missing_ok=True)


def test_generate_dashboard(temp_db_with_data):
    dashboard = generate_dashboard(temp_db_with_data, days=7, top_n=10)

    assert "generated_at" in dashboard
    assert "window_days" in dashboard
    assert dashboard["window_days"] == 7
    assert "summary" in dashboard
    assert "total_models" in dashboard["summary"]
    assert "active_models" in dashboard["summary"]
    assert "free_models" in dashboard["summary"]
    assert "aliases" in dashboard["summary"]
    assert "providers_monitored" in dashboard["summary"]

    assert "top_movers" in dashboard
    assert "stale_entries" in dashboard
    assert "provider_health" in dashboard
    assert "alias_status" in dashboard
    assert "stability_report" in dashboard
    assert "rankings" in dashboard

    # Check rankings for each task
    for task in ["coding", "reasoning", "long-context", "vision", "bulk", "fast"]:
        assert task in dashboard["rankings"]
        assert isinstance(dashboard["rankings"][task], list)


def test_generate_dashboard_empty_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    init_db(db_path)
    try:
        dashboard = generate_dashboard(db_path)
        assert dashboard["summary"]["total_models"] == 0
        assert dashboard["summary"]["active_models"] == 0
        assert dashboard["summary"]["free_models"] == 0
        assert dashboard["summary"]["aliases"] == 0
    finally:
        db_path.unlink(missing_ok=True)


def test_generate_html_dashboard(temp_db_with_data):
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        html_path = Path(f.name)
    html_path.unlink(missing_ok=True)

    try:
        result = generate_html_dashboard(temp_db_with_data, days=7, top_n=10, output_path=html_path)
        assert result == str(html_path)
        assert html_path.exists()

        content = html_path.read_text()
        assert "<html>" in content
        assert "Free Model Radar Dashboard" in content
        assert "free-best-coding" in content
        assert "model-a" in content
        assert "provider_health" in content.lower() or "Provider Health" in content
    finally:
        html_path.unlink(missing_ok=True)


def test_generate_html_dashboard_string_return(temp_db_with_data):
    html = generate_html_dashboard(temp_db_with_data, days=7, top_n=10)
    assert isinstance(html, str)
    assert "<html>" in html
    assert "Free Model Radar Dashboard" in html


def test_dashboard_stability_report(temp_db_with_data):
    dashboard = generate_dashboard(temp_db_with_data, days=7)
    stability = dashboard["stability_report"]

    assert "window_days" in stability
    assert "since" in stability
    assert "generated_at" in stability
    assert "aliases" in stability
    assert len(stability["aliases"]) >= 1

    alias_info = stability["aliases"][0]
    assert "alias" in alias_info
    assert "provider" in alias_info
    assert "model_id" in alias_info
    assert "score" in alias_info
    assert "probes" in alias_info
    assert "success_rate" in alias_info
    assert "alias_changes" in alias_info
    assert "stable" in alias_info


def test_dashboard_provider_health(temp_db_with_data):
    dashboard = generate_dashboard(temp_db_with_data)
    ph = dashboard["provider_health"]
    assert len(ph) >= 1
    p = ph[0]
    assert "provider" in p
    assert "health" in p
    assert "free_quota" in p
    assert "rate_limit" in p
    assert "updated_at" in p


def test_dashboard_top_movers(temp_db_with_data):
    dashboard = generate_dashboard(temp_db_with_data)
    movers = dashboard["top_movers"]
    assert isinstance(movers, list)
    if len(movers) > 0:
        m = movers[0]
        assert "provider" in m
        assert "model_id" in m
        assert "probes" in m
        assert "successes" in m
        assert "success_rate" in m
        assert "first_ts" in m
        assert "last_ts" in m


def test_dashboard_stale_entries(temp_db_with_data):
    dashboard = generate_dashboard(temp_db_with_data)
    stale = dashboard["stale_entries"]
    assert isinstance(stale, list)
    # Models were just added and probed, so none should be stale
    for s in stale:
        assert "provider" in s
        assert "model_id" in s
        assert "last_verified" in s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])