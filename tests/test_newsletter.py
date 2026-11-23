"""Tests for the weekly newsletter draft generator."""

from __future__ import annotations

import json

from freeradar.newsletter import build, diff_snapshots, load_snapshot, render


def _snap(models, generated_at="2026-01-01T00:00:00+00:00"):
    return {
        "generated_at": generated_at,
        "provider_count": len({k.split("/", 1)[0] for k in models}),
        "models": models,
    }


def _m(health, tps=None, tools=False):
    return {"health": health, "throughput": tps, "context": 4096, "tool_calls": tools}


def test_self_test_passes():
    from freeradar.newsletter import _self_test

    assert _self_test() == 0


def test_first_run_reports_no_movers():
    new = _snap({"a/one": _m("healthy", 10.0)})
    d = diff_snapshots(None, new)
    assert d["is_first_run"] is True
    assert d["added"] == [] and d["removed"] == []
    assert d["newly_healthy"] == [] and d["newly_broken"] == []


def test_movers_classified():
    old = _snap({"a/up": _m("unhealthy"), "a/down": _m("healthy", 5.0), "a/gone": _m("healthy")})
    new = _snap({"a/up": _m("healthy", 9.0), "a/down": _m("unhealthy"), "a/new": _m("unknown")})
    d = diff_snapshots(old, new)
    assert d["newly_healthy"] == ["a/up"]
    assert d["newly_broken"] == ["a/down"]
    assert d["added"] == ["a/new"]
    assert d["removed"] == ["a/gone"]
    assert d["fastest"] == [(9.0, "a/up")]


def test_render_contains_counts_and_no_placeholder_junk():
    new = _snap({"a/one": _m("healthy", 10.0, tools=True), "a/two": _m("degraded")})
    text = render(diff_snapshots(None, new), new["generated_at"])
    assert "Verified healthy | 1" in text
    assert "Tool-capable and healthy | 1" in text
    assert "First run" in text
    assert "spendwarden" in text


def test_build_is_deterministic_and_state_advances_only_on_commit(tmp_path):
    data = {
        "generated_at": "2026-11-21T00:00:00+00:00",
        "models": [
            {"provider": "p", "model_id": "m1", "health": "healthy", "throughput": 3.0, "context": 1024, "tool_calls": False}
        ],
    }
    src = tmp_path / "free_models.json"
    src.write_text(json.dumps(data))
    state = tmp_path / "state.json"
    out = tmp_path / "drafts"

    draft = build(src, state, out, commit=False)
    assert draft.exists()
    # first run has no mover lists, so the model shows up in the speed table
    assert "| `m1` | p | 3.0 |" in draft.read_text()
    assert not state.exists()

    build(src, state, out, commit=True)
    assert json.loads(state.read_text())["models"]["p/m1"]["health"] == "healthy"

    second = build(src, state, out, commit=False)
    assert "First run" not in second.read_text()


def test_load_snapshot_reads_real_shape(tmp_path):
    p = tmp_path / "fm.json"
    p.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-21T01:23:21+00:00",
                "models": [
                    {"provider": "x", "model_id": "y", "health": "degraded", "throughput": None, "context": 8192, "tool_calls": True}
                ],
            }
        )
    )
    s = load_snapshot(p)
    assert s["models"]["x/y"] == {"health": "degraded", "throughput": None, "context": 8192, "tool_calls": True}
    assert s["provider_count"] == 1
