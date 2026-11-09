"""Verification probes for free models.

Runs tiny chat + tool-call probe per model. Records:
- Status code (200/401/402/429/5xx)
- Latency
- Tokens per second
- Respects Retry-After header
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from freeradar.registry import (
    connect,
    get_model,
    list_models,
    record_probe,
    apply_probe_outcome,
    set_provider_health,
    in_cooldown,
    HEALTH_HEALTHY,
    HEALTH_DEGRADED,
    HEALTH_UNHEALTHY,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.json"
if not OPENCODE_JSON.exists():
    OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.jsonc"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_CONCURRENT_PROBES = 5
PROBE_CHAT_MESSAGE = "Reply with just: ok"
PROBE_TOOL_CALL_MESSAGE = "What is 2+2? Use the calculate tool if available."


@dataclass
class ProbeResult:
    provider: str
    model_id: str
    probe_type: str  # "chat" or "tool_call"
    ok: bool
    status_code: int | None
    latency_ms: float | None
    tokens: int | None
    tps: float | None
    error: str | None
    retry_after: float | None


def _parse_jsonc(text: str) -> Any:
    import json
    import re
    text = re.sub(r"(^|[{}\s])//.*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return json.loads(text)


def _resolve_env_var(s: str) -> str:
    import re
    if not s:
        return s
    s = re.sub(
        r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: os.environ.get(m.group(1), ""),
        s,
    )
    s = re.sub(
        r"\{file:([^}]+)\}",
        lambda m: Path(os.path.expanduser(m.group(1))).read_text().strip() if Path(os.path.expanduser(m.group(1))).exists() else "",
        s,
    )
    return os.path.expandvars(s)


def _load_provider_config(provider: str) -> tuple[str, str] | None:
    """Load base_url and api_key for a provider from opencode.json."""
    if not OPENCODE_JSON.exists():
        return None
    data = _parse_jsonc(OPENCODE_JSON.read_text())
    providers = data.get("provider", {})
    if provider not in providers:
        return None
    pconfig = providers[provider]
    base_url = pconfig.get("baseURL", "")
    api_key = pconfig.get("apiKey", "")
    options = pconfig.get("options", {})
    if not base_url and options:
        base_url = options.get("baseURL", "")
    if not api_key and options:
        api_key = options.get("apiKey", "")
    api_key = _resolve_env_var(api_key)
    if not base_url:
        return None
    return base_url, api_key


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return max(1, len(text) // 4)


async def _probe_chat(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_id: str,
    stream: bool = False,
) -> ProbeResult:
    """Run a tiny chat completion probe."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROBE_CHAT_MESSAGE}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": stream,
    }

    start = time.time()
    status_code: int | None = None
    latency_ms: float | None = None
    tokens: int | None = None
    tps: float | None = None
    error: str | None = None
    retry_after: float | None = None
    ok = False

    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        status_code = resp.status_code
        latency_ms = (time.time() - start) * 1000

        if status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "60"))
            error = "rate limited"
        elif status_code >= 500:
            error = f"server error {status_code}"
        elif status_code in (401, 402, 403):
            error = f"auth error {status_code}"
        elif status_code != 200:
            error = f"http {status_code}"
        else:
            if stream:
                # For streaming, count chunks
                content = ""
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        content += chunk.decode("utf-8", errors="ignore")
                tokens = _estimate_tokens(content)
            else:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                tokens = usage.get("completion_tokens") or usage.get("output_tokens") or _estimate_tokens(content)

            if tokens and latency_ms:
                tps = tokens / (latency_ms / 1000)

            ok = True
            error = None

    except httpx.TimeoutException:
        latency_ms = (time.time() - start) * 1000
        error = "timeout"
        status_code = None
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        error = f"{type(e).__name__}: {e}"
        status_code = None

    return ProbeResult(
        provider="",
        model_id="",
        probe_type="chat",
        ok=ok,
        status_code=status_code,
        latency_ms=latency_ms,
        tokens=tokens,
        tps=tps,
        error=error,
        retry_after=retry_after,
    )


async def _probe_tool_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_id: str,
) -> ProbeResult:
    """Run a tool-call probe (if model supports it)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"} if api_key else {"Content-Type": "application/json"}
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROBE_TOOL_CALL_MESSAGE}],
        "max_tokens": 64,
        "temperature": 0,
        "tools": [{
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "Calculate a math expression",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Math expression to evaluate"}
                    },
                    "required": ["expression"]
                }
            }
        }],
        "tool_choice": "auto",
    }

    start = time.time()
    status_code: int | None = None
    latency_ms: float | None = None
    tokens: int | None = None
    tps: float | None = None
    error: str | None = None
    retry_after: float | None = None
    ok = False

    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        status_code = resp.status_code
        latency_ms = (time.time() - start) * 1000

        if status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "60"))
            error = "rate limited"
        elif status_code >= 500:
            error = f"server error {status_code}"
        elif status_code in (401, 402, 403):
            error = f"auth error {status_code}"
        elif status_code != 200:
            error = f"http {status_code}"
        else:
            data = resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls")
            content = message.get("content", "")
            usage = data.get("usage", {})
            tokens = usage.get("completion_tokens") or usage.get("output_tokens") or _estimate_tokens(content)

            if tool_calls:
                ok = True
                error = None
            else:
                # Model doesn't support tool calls - not a failure, just not capable
                ok = True
                error = "no_tool_call_support"

            if tokens and latency_ms:
                tps = tokens / (latency_ms / 1000)

    except httpx.TimeoutException:
        latency_ms = (time.time() - start) * 1000
        error = "timeout"
        status_code = None
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        error = f"{type(e).__name__}: {e}"
        status_code = None

    return ProbeResult(
        provider="",
        model_id="",
        probe_type="tool_call",
        ok=ok,
        status_code=status_code,
        latency_ms=latency_ms,
        tokens=tokens,
        tps=tps,
        error=error,
        retry_after=retry_after,
    )


async def verify_model(
    provider: str,
    model_id: str,
    db_path: Path | None = None,
) -> dict[str, ProbeResult]:
    """Verify a single model with chat and tool-call probes."""
    config = _load_provider_config(provider)
    if not config:
        return {}

    base_url, api_key = config

    conn = connect(db_path)
    model = get_model(conn, provider, model_id)
    conn.close()

    if not model or not model.get("free_tier", True):
        return {}

    # Check provider cooldown
    conn = connect(db_path)
    if in_cooldown(conn, provider):
        conn.close()
        return {}
    conn.close()

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        # Chat probe
        chat_result = await _probe_chat(client, base_url, api_key, model_id)
        chat_result.provider = provider
        chat_result.model_id = model_id

        # Tool call probe (only if model claims support)
        tool_result = None
        if model.get("tool_calls", False):
            tool_result = await _probe_tool_call(client, base_url, api_key, model_id)
            tool_result.provider = provider
            tool_result.model_id = model_id

    # Record results
    conn = connect(db_path)
    record_probe(conn, provider, model_id, chat_result.probe_type, chat_result.ok,
                 chat_result.status_code, chat_result.latency_ms, chat_result.tokens,
                 chat_result.tps, chat_result.error, chat_result.retry_after)
    apply_probe_outcome(conn, provider, model_id, chat_result.ok,
                        chat_result.latency_ms, chat_result.tokens, chat_result.tps,
                        chat_result.status_code)
    if tool_result:
        record_probe(conn, provider, model_id, tool_result.probe_type, tool_result.ok,
                     tool_result.status_code, tool_result.latency_ms, tool_result.tokens,
                     tool_result.tps, tool_result.error, tool_result.retry_after)
        # Tool call outcome doesn't affect health directly, just recorded
    conn.commit()
    conn.close()

    results = {"chat": chat_result}
    if tool_result:
        results["tool_call"] = tool_result

    # Update provider health on rate limit
    if chat_result.status_code == 429 and chat_result.retry_after:
        conn = connect(db_path)
        reset_at = datetime.fromtimestamp(time.time() + chat_result.retry_after, tz=timezone.utc).isoformat()
        set_provider_health(conn, provider, HEALTH_DEGRADED, rate_limit_reset_at=reset_at)
        conn.commit()
        conn.close()

    return results


async def _verify_worker(
    queue: asyncio.Queue[tuple[str, str]],
    results: dict[str, dict[str, ProbeResult]],
    db_path: Path | None,
    semaphore: asyncio.Semaphore,
) -> None:
    while True:
        try:
            provider, model_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with semaphore:
            try:
                result = await verify_model(provider, model_id, db_path)
                if result:
                    results[f"{provider}/{model_id}"] = result
            except Exception as e:
                results[f"{provider}/{model_id}"] = {"error": ProbeResult(
                    provider=provider, model_id=model_id, probe_type="error",
                    ok=False, status_code=None, latency_ms=None, tokens=None,
                    tps=None, error=str(e), retry_after=None
                )}
        queue.task_done()


async def verify_all(
    db_path: Path | None = None,
    provider_filter: str | None = None,
    model_filter: str | None = None,
    max_concurrent: int = MAX_CONCURRENT_PROBES,
    limit: int | None = None,
) -> dict[str, dict[str, ProbeResult]]:
    """Verify all active free models in the registry."""
    conn = connect(db_path)
    models = list_models(conn, active_only=True, free_only=True)
    conn.close()

    if provider_filter:
        import fnmatch
        models = [m for m in models if fnmatch.fnmatch(m["provider"].lower(), provider_filter.lower())]
    if model_filter:
        import fnmatch
        models = [m for m in models if fnmatch.fnmatch(m["model_id"].lower(), model_filter.lower())]
    if limit:
        models = models[:limit]

    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    for m in models:
        queue.put_nowait((m["provider"], m["model_id"]))

    results: dict[str, dict[str, ProbeResult]] = {}
    semaphore = asyncio.Semaphore(max_concurrent)

    workers = [
        asyncio.create_task(_verify_worker(queue, results, db_path, semaphore))
        for _ in range(min(max_concurrent, queue.qsize()))
    ]

    await queue.join()
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    return results


def verify_all_sync(
    db_path: Path | None = None,
    provider_filter: str | None = None,
    model_filter: str | None = None,
    max_concurrent: int = MAX_CONCURRENT_PROBES,
    limit: int | None = None,
) -> dict[str, dict[str, ProbeResult]]:
    return asyncio.run(verify_all(db_path, provider_filter, model_filter, max_concurrent, limit))


if __name__ == "__main__":
    import json
    import sys
    result = verify_all_sync()
    print(json.dumps({k: {pk: vars(pv) for pk, pv in v.items()} for k, v in result.items()}, indent=2, default=str))
    sys.exit(0)