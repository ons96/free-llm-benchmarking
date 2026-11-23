"""Tests for freeradar.verify module."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from freeradar.verify import (
    _parse_jsonc,
    _resolve_env_var,
    _load_provider_config,
    _estimate_tokens,
    _probe_chat,
    _probe_tool_call,
    verify_model,
    verify_all_sync,
    ProbeResult,
)


def test_parse_jsonc():
    text = '{"provider": {"baseURL": "https://test.com", "apiKey": "key123"}}'
    result = _parse_jsonc(text)
    assert result["provider"]["baseURL"] == "https://test.com"


def test_resolve_env_var(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "secret123")
    assert _resolve_env_var("${TEST_API_KEY}") == "secret123"
    assert _resolve_env_var("$TEST_API_KEY") == "secret123"
    assert _resolve_env_var("{env:TEST_API_KEY}") == "secret123"
    assert _resolve_env_var("plain") == "plain"


def test_estimate_tokens():
    assert _estimate_tokens("hello") == 1  # 5 chars / 4 = 1
    assert _estimate_tokens("hello world") == 2  # 11 chars / 4 = 2
    assert _estimate_tokens("") == 1  # minimum 1


def test_load_provider_config():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"provider": {"test-provider": {"baseURL": "https://test.com", "apiKey": "key123"}}}')
        config_path = Path(f.name)

    try:
        import freeradar.verify as verify_module
        original = verify_module.OPENCODE_JSON
        verify_module.OPENCODE_JSON = config_path

        config = _load_provider_config("test-provider")
        assert config == ("https://test.com", "key123")

        config = _load_provider_config("unknown-provider")
        assert config is None

        verify_module.OPENCODE_JSON = original
    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_probe_chat_success():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"completion_tokens": 10}
    }
    mock_client.post.return_value = mock_resp

    result = await _probe_chat(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is True
    assert result.status_code == 200
    assert result.tokens == 10
    assert result.tps is not None
    assert result.error is None


@pytest.mark.asyncio
async def test_probe_chat_rate_limit():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "60"}
    mock_client.post.return_value = mock_resp

    result = await _probe_chat(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is False
    assert result.status_code == 429
    assert result.retry_after == 60.0
    assert "rate limited" in result.error


@pytest.mark.asyncio
async def test_probe_chat_auth_error():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_client.post.return_value = mock_resp

    result = await _probe_chat(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is False
    assert result.status_code == 401
    assert "auth error" in result.error


@pytest.mark.asyncio
async def test_probe_chat_server_error():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_client.post.return_value = mock_resp

    result = await _probe_chat(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is False
    assert result.status_code == 500
    assert "server error" in result.error


@pytest.mark.asyncio
async def test_probe_chat_timeout():
    import httpx
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.TimeoutException("timeout")

    result = await _probe_chat(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is False
    assert result.status_code is None
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_probe_tool_call_success():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "4", "tool_calls": [{"function": {"name": "calculate"}}]}}],
        "usage": {"completion_tokens": 20}
    }
    mock_client.post.return_value = mock_resp

    result = await _probe_tool_call(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is True
    assert result.status_code == 200
    assert result.tokens == 20


@pytest.mark.asyncio
async def test_probe_tool_call_no_tool_support():
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "4"}}],
        "usage": {"completion_tokens": 10}
    }
    mock_client.post.return_value = mock_resp

    result = await _probe_tool_call(mock_client, "https://test.com", "key", "model-1")
    assert result.ok is True
    assert result.status_code == 200
    assert "no_tool_call_support" in result.error


def test_verify_model_no_config():
    import asyncio
    result = asyncio.run(verify_model("unknown-provider", "model-1"))
    assert result == {}


def test_verify_model_not_free_tier():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    from freeradar.registry import init_db, upsert_model, connect
    init_db(db_path)
    conn = connect(db_path)
    upsert_model(conn, "test-provider", "model-1")
    # Set free_tier directly via SQL since it's not in DISCOVERY_FIELDS
    conn.execute("UPDATE models SET free_tier = 0 WHERE provider = ? AND model_id = ?", ("test-provider", "model-1"))
    conn.commit()
    conn.close()

    try:
        import freeradar.verify as verify_module
        original = verify_module.OPENCODE_JSON
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"provider": {"test-provider": {"baseURL": "https://test.com", "apiKey": "key123"}}}')
            verify_module.OPENCODE_JSON = Path(f.name)

        import asyncio
        result = asyncio.run(verify_model("test-provider", "model-1", db_path))
        assert result == {}

        verify_module.OPENCODE_JSON = original
    finally:
        db_path.unlink(missing_ok=True)


def test_verify_all_sync():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    from freeradar.registry import init_db, upsert_model, connect
    init_db(db_path)
    conn = connect(db_path)
    upsert_model(conn, "p1", "m1", {"free_tier": True, "tool_calls": False})
    upsert_model(conn, "p2", "m2", {"free_tier": True, "tool_calls": True})
    conn.commit()
    conn.close()

    try:
        with patch("freeradar.verify.verify_model") as mock_verify:
            mock_verify.return_value = {
                "chat": ProbeResult("p1", "m1", "chat", True, 200, 100.0, 10, 100.0, None, None)
            }
            result = verify_all_sync(db_path, limit=10)
            assert "p1/m1" in result
            assert "p2/m2" in result
    finally:
        db_path.unlink(missing_ok=True)


def test_probe_result_dataclass():
    pr = ProbeResult(
        provider="p1",
        model_id="m1",
        probe_type="chat",
        ok=True,
        status_code=200,
        latency_ms=100.0,
        tokens=10,
        tps=100.0,
        error=None,
        retry_after=None,
    )
    assert pr.provider == "p1"
    assert pr.ok is True
    assert pr.tps == 100.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])