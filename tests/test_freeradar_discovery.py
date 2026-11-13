"""Tests for freeradar.discovery module."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.discovery import (
    _parse_jsonc,
    _resolve_env_var,
    _is_model_free,
    _fetch_json,
    discover_from_opencode,
    discover_from_openrouter,
    discover_from_models_dev,
    discover_from_github,
    discover_stealth_mentions,
    run_discovery_sync,
    DISCOVERY_SOURCES,
)


def test_parse_jsonc():
    text = '{"key": "value" // comment\n}'
    result = _parse_jsonc(text)
    assert result == {"key": "value"}

    text = '{"key": "value" /* comment */}'
    result = _parse_jsonc(text)
    assert result == {"key": "value"}

    text = '{"key": "value"}'
    result = _parse_jsonc(text)
    assert result == {"key": "value"}


def test_resolve_env_var(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "test_value")
    assert _resolve_env_var("${TEST_KEY}") == "test_value"
    assert _resolve_env_var("$TEST_KEY") == "test_value"
    assert _resolve_env_var("{env:TEST_KEY}") == "test_value"
    assert _resolve_env_var("plain") == "plain"
    assert _resolve_env_var("") == ""


def test_is_model_free():
    assert _is_model_free("provider", "gpt-4-free") is True
    assert _is_model_free("provider", "model:free") is True
    assert _is_model_free("provider", "big-pickle-model") is True
    assert _is_model_free("provider", "gpt-4") is False
    assert _is_model_free("kilo", "free-model") is True
    assert _is_model_free("kilo", "paid-model") is False


@pytest.mark.asyncio
async def test_fetch_json_success():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"id": "model-1"}]}
    mock_client.get.return_value = mock_resp

    result = await _fetch_json(mock_client, "https://example.com/models")
    assert result == {"data": [{"id": "model-1"}]}


@pytest.mark.asyncio
async def test_fetch_json_failure():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_client.get.return_value = mock_resp

    result = await _fetch_json(mock_client, "https://example.com/models")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_json_exception():
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("network error")

    result = await _fetch_json(mock_client, "https://example.com/models")
    assert result is None


def test_discover_stealth_mentions():
    import asyncio
    models = list(asyncio.run(discover_stealth_mentions()))
    assert len(models) >= 4
    for m in models:
        assert "provider" in m
        assert "model_id" in m
        assert "context" in m
        assert "modalities" in m
        assert "tool_calls" in m
        assert "free_quota" in m
        assert "source_url" in m


def test_run_discovery_sync_creates_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    db_path.unlink(missing_ok=True)

    try:
        with patch("freeradar.discovery.discover_from_opencode", return_value=iter([])):
            with patch("freeradar.discovery.discover_from_openrouter", return_value=iter([])):
                with patch("freeradar.discovery.discover_from_models_dev", return_value=iter([])):
                    with patch("freeradar.discovery.discover_from_github", return_value=iter([])):
                        with patch("freeradar.discovery.discover_stealth_mentions") as mock_stealth:
                            mock_stealth.return_value = iter([
                                {
                                    "provider": "test-provider",
                                    "model_id": "test-model",
                                    "context": 4096,
                                    "modalities": ["text"],
                                    "tool_calls": False,
                                    "free_quota": "unlimited",
                                    "rate_limit": "10",
                                    "reset_window": "1m",
                                    "quality_score": 0.5,
                                    "source_url": "https://test.com",
                                    "tos_notes": "test",
                                }
                            ])
                            result = run_discovery_sync(db_path)

        assert result["stealth_mentions"] == 1
        assert db_path.exists()

        # Verify model in DB
        from freeradar.registry import connect, get_model
        conn = connect(db_path)
        model = get_model(conn, "test-provider", "test-model")
        conn.close()
        assert model is not None
        assert model["provider"] == "test-provider"
        assert model["model_id"] == "test-model"
    finally:
        db_path.unlink(missing_ok=True)


def test_discovery_sources_constant():
    expected = ("opencode", "openrouter", "models_dev", "github", "stealth_mentions", "manual")
    assert DISCOVERY_SOURCES == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])