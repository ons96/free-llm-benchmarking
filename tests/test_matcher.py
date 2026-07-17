"""Tests for matcher.py JSON-driven alias loading (issue #334 consumer side)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import matcher


def test_normalize_uses_json_aliases():
    """normalize() should resolve vendor aliases from config/model_alias_mapping.json."""
    # Force reload in case a prior test cached an empty map.
    matcher._ALIAS_MAP = None
    assert matcher.normalize("deepseek-chat") == "deepseek-v3"
    assert matcher.normalize("kimi-k2-0905") == "kimi-k2"
    assert matcher.normalize("gpt-5-0806") == "gpt-5"


def test_normalize_strips_provider_prefix():
    matcher._ALIAS_MAP = None
    assert matcher.normalize("openai/gpt-5") == "gpt-5"
    assert matcher.normalize("anthropic/claude-opus-4-5") == "claude-opus-4.5"


def test_normalize_unknown_model_falls_to_heuristic():
    matcher._ALIAS_MAP = None
    # Not in alias map -> heuristic normalization (suffix strip + separator norm).
    assert matcher.normalize("some-unknown-model-20250618") == "some-unknown-model"


def test_normalize_empty_and_whitespace():
    matcher._ALIAS_MAP = None
    assert matcher.normalize("") == ""
    assert matcher.normalize("   ") == ""


def test_load_aliases_fail_open_missing_file(tmp_path):
    matcher._ALIAS_MAP = None
    m = matcher._load_aliases(tmp_path / "nonexistent.json", force=True)
    assert m == {}


def test_load_aliases_fail_open_malformed(tmp_path):
    matcher._ALIAS_MAP = None
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    m = matcher._load_aliases(p, force=True)
    assert m == {}


def test_load_aliases_returns_normalized_keys(tmp_path):
    matcher._ALIAS_MAP = None
    import json
    p = tmp_path / "test.json"
    p.write_text(json.dumps({
        "version": 1,
        "groups": [{"canonical": "DeepSeek-V3", "aliases": ["deepseek_chat", "deepseek-chat-v3"]}],
    }), encoding="utf-8")
    m = matcher._load_aliases(p, force=True)
    assert m["deepseek-v3"] == "deepseek-v3"
    assert m["deepseek-chat"] == "deepseek-v3"  # underscore normalized
    assert m["deepseek-chat-v3"] == "deepseek-v3"


def test_real_config_file_loads():
    """The shipped config/model_alias_mapping.json must load with entries."""
    matcher._ALIAS_MAP = None
    m = matcher._load_aliases(force=True)
    assert len(m) > 0
    # normalize() is the public API; deepseek-chat resolves to deepseek-v3
    # (the -chat suffix is stripped by _normalize_no_alias before map lookup,
    # so the raw key is 'deepseek', but normalize() handles that internally).
    assert matcher.normalize("deepseek-chat") == "deepseek-v3"
