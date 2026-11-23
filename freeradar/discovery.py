"""Discovery sources for free models.

Fetches from:
- Provider /v1/models endpoints (configured in opencode.json)
- OpenRouter free model list
- models.dev
- GitHub releases / community lists
- Static stealth model mentions
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from freeradar.registry import (
    connect,
    init_db,
    upsert_model,
    log_discovery,
    HEALTH_UNKNOWN,
    MODEL_FIELDS,
    DISCOVERY_FIELDS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.json"
if not OPENCODE_JSON.exists():
    OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.jsonc"

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
DISCOVERY_SOURCES = (
    "opencode",
    "openrouter",
    "models_dev",
    "github",
    "stealth_mentions",
    "manual",
)

SKIP_PROVIDERS = {
    "brave_search",
    "duckduckgo",
    "exa",
    "jina",
    "tavily",
    "cliproxyapi",
    "supacoder",
    "zenllm",
    "swiftrouter",
    "zenmux",
    "llmgateway",
    "kilocloud",
    "claude-carter",
    "cortecs",
    "g4f",
    "g4f_gemini",
    "g4f_groq",
    "g4f_nvidia",
    "g4f_ollama",
    "g4f_pollinations",
    "antigravity",
    "iflow",
    "noobrouter",
}

NO_STREAM_PROVIDERS = {"bluesminds"}

FREE_MODEL_PATTERNS = ["free", "big-pickle"]

PROVIDER_REGISTRY_PATH = REPO_ROOT / "data" / "provider_registry.json"


def _parse_jsonc(text: str) -> Any:
    text = re.sub(r"(^|[{}\s])//.*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return json.loads(text)


def _resolve_env_var(s: str) -> str:
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


def _is_model_free(provider_name: str, model_name: str) -> bool:
    name_lower = model_name.lower()
    return any(pat in name_lower for pat in FREE_MODEL_PATTERNS)


async def _fetch_json(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> Any:
    try:
        resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


async def discover_from_opencode() -> AsyncIterator[dict[str, Any]]:
    """Discover models from providers configured in opencode.json."""
    if not OPENCODE_JSON.exists():
        return
    data = _parse_jsonc(OPENCODE_JSON.read_text())
    providers = data.get("provider", {})

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for pname, pconfig in providers.items():
            if pname in SKIP_PROVIDERS:
                continue

            base_url = pconfig.get("baseURL", "")
            api_key = pconfig.get("apiKey", "")
            options = pconfig.get("options", {})
            if not base_url and options:
                base_url = options.get("baseURL", "")
            if not api_key and options:
                api_key = options.get("apiKey", "")

            api_key = _resolve_env_var(api_key)
            if not base_url:
                continue

            models_url = f"{base_url.rstrip('/')}/models"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            result = await _fetch_json(client, models_url, headers)
            if not result:
                continue

            models_data = result.get("data", result) if isinstance(result, dict) else result
            for m in models_data:
                mid = m.get("id", "")
                if not mid:
                    continue

                model_type = m.get("model_type", "").lower()
                if model_type in {"image", "vision", "tts", "speech", "audio", "safety", "content-safety", "moderation", "embed", "rerank"}:
                    continue

                modalities = ["text"]
                if model_type in {"vision", "image"}:
                    modalities.append("vision")
                caps = m.get("capabilities", {})
                tool_calls = bool(caps.get("function_calling") or "function" in str(caps))

                context = m.get("context_length") or m.get("max_tokens") or 4096
                try:
                    context = int(context)
                except (TypeError, ValueError):
                    context = 4096

                free_quota = None
                rate_limit = None
                reset_window = None
                tos_notes = None

                yield {
                    "provider": pname,
                    "model_id": mid,
                    "context": context,
                    "modalities": modalities,
                    "tool_calls": tool_calls,
                    "free_quota": free_quota,
                    "rate_limit": rate_limit,
                    "reset_window": reset_window,
                    "quality_score": None,
                    "source_url": base_url,
                    "tos_notes": tos_notes,
                }


async def discover_from_openrouter() -> AsyncIterator[dict[str, Any]]:
    """Discover free models from OpenRouter free list."""
    url = "https://openrouter.ai/api/v1/models"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        result = await _fetch_json(client, url)
        if not result:
            return

        models_data = result.get("data", result) if isinstance(result, dict) else result
        for m in models_data:
            mid = m.get("id", "")
            if not mid:
                continue

            pricing = m.get("pricing", {})
            prompt_price = pricing.get("prompt", "0")
            completion_price = pricing.get("completion", "0")
            try:
                is_free = float(prompt_price) == 0 and float(completion_price) == 0
            except (ValueError, TypeError):
                is_free = ":free" in mid.lower() or "free" in mid.lower()

            if not is_free:
                continue

            context = m.get("context_length", 4096)
            try:
                context = int(context)
            except (TypeError, ValueError):
                context = 4096

            modalities = ["text"]
            if "vision" in mid.lower() or "image" in mid.lower():
                modalities.append("vision")

            supported_params = m.get("supported_parameters", {})
            if isinstance(supported_params, dict):
                tool_calls = bool(supported_params.get("tools"))
            elif isinstance(supported_params, list):
                tool_calls = "tools" in supported_params
            else:
                tool_calls = False

            yield {
                "provider": "openrouter",
                "model_id": mid,
                "context": context,
                "modalities": modalities,
                "tool_calls": tool_calls,
                "free_quota": "daily",
                "rate_limit": None,
                "reset_window": "24h",
                "quality_score": None,
                "source_url": "https://openrouter.ai/api/v1/models",
                "tos_notes": "Free tier per OpenRouter pricing",
            }


async def discover_from_models_dev() -> AsyncIterator[dict[str, Any]]:
    """Discover models from models.dev (community curated)."""
    url = "https://models.dev/api.json"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        result = await _fetch_json(client, url)
        if not result:
            return

        for provider_name, provider_data in result.items():
            if "models" not in provider_data:
                continue

            models = provider_data["models"]
            if isinstance(models, dict):
                # models is a dict {model_name: model_info}
                items = models.items()
            elif isinstance(models, list):
                # models is a list of model_info dicts
                items = [(None, m) for m in models]
            else:
                continue

            for _name, m in items:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id", "") or m.get("name", "")
                if not mid:
                    continue

                pricing = m.get("pricing", {})
                is_free = pricing.get("free", False) or pricing.get("input", 0) == 0
                if not is_free:
                    continue

                context = m.get("context", 4096)
                try:
                    context = int(context)
                except (TypeError, ValueError):
                    context = 4096

                modalities = ["text"]
                if m.get("modalities", {}).get("image") or "vision" in mid.lower():
                    modalities.append("vision")

                tool_calls = bool(m.get("capabilities", {}).get("tools"))

                yield {
                    "provider": provider_name,
                    "model_id": mid,
                    "context": context,
                    "modalities": modalities,
                    "tool_calls": tool_calls,
                    "free_quota": "unknown",
                    "rate_limit": None,
                    "reset_window": "unknown",
                    "quality_score": None,
                    "source_url": "https://models.dev",
                    "tos_notes": "Community curated",
                }


async def discover_from_github() -> AsyncIterator[dict[str, Any]]:
    """Discover from known GitHub awesome lists / community repos."""
    known_repos = [
        "https://raw.githubusercontent.com/ahmp/awesome-free-llm-apis/main/models.json",
        "https://raw.githubusercontent.com/cheahjs/free-llm-api-resources/main/data/models.json",
    ]

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        for url in known_repos:
            result = await _fetch_json(client, url)
            if not result:
                continue

            if isinstance(result, list):
                items = result
            elif isinstance(result, dict):
                items = result.get("models", result.get("data", []))
            else:
                continue

            for m in items:
                mid = m.get("id", m.get("model", m.get("name", "")))
                provider = m.get("provider", m.get("source", "github"))
                if not mid:
                    continue

                context = m.get("context", m.get("context_length", 4096))
                try:
                    context = int(context)
                except (TypeError, ValueError):
                    context = 4096

                modalities = ["text"]
                if m.get("vision") or "vision" in mid.lower():
                    modalities.append("vision")

                tool_calls = bool(m.get("tools") or m.get("function_calling"))

                yield {
                    "provider": provider,
                    "model_id": mid,
                    "context": context,
                    "modalities": modalities,
                    "tool_calls": tool_calls,
                    "free_quota": m.get("quota", "unknown"),
                    "rate_limit": m.get("rate_limit"),
                    "reset_window": m.get("reset_window", "unknown"),
                    "quality_score": None,
                    "source_url": url,
                    "tos_notes": "GitHub community list",
                }


async def discover_stealth_mentions() -> list[dict[str, Any]]:
    """Static list of known stealth/undocumented free models."""
    stealth_models = [
        {
            "provider": "blazeai",
            "model_id": "qwen2.5-coder-32b-instruct",
            "context": 32768,
            "modalities": ["text"],
            "tool_calls": True,
            "free_quota": "unlimited",
            "rate_limit": "20",
            "reset_window": "1m",
            "quality_score": 0.75,
            "source_url": "https://blazeapi.org",
            "tos_notes": "Unlimited free tier",
        },
        {
            "provider": "cerebras",
            "model_id": "llama3.1-70b",
            "context": 8192,
            "modalities": ["text"],
            "tool_calls": True,
            "free_quota": "1M tokens/day",
            "rate_limit": "30",
            "reset_window": "1m",
            "quality_score": 0.8,
            "source_url": "https://cerebras.ai",
            "tos_notes": "Free tier 1M tokens/day",
        },
        {
            "provider": "groq",
            "model_id": "llama-3.1-70b-versatile",
            "context": 131072,
            "modalities": ["text"],
            "tool_calls": True,
            "free_quota": "daily",
            "rate_limit": "30",
            "reset_window": "1m",
            "quality_score": 0.85,
            "source_url": "https://groq.com",
            "tos_notes": "Free tier with rate limits",
        },
        {
            "provider": "nvidia",
            "model_id": "nemotron-3-ultra",
            "context": 4096,
            "modalities": ["text"],
            "tool_calls": True,
            "free_quota": "unlimited",
            "rate_limit": "40",
            "reset_window": "1m",
            "quality_score": 0.9,
            "source_url": "https://integrate.api.nvidia.com",
            "tos_notes": "NIM free tier",
        },
    ]

    return stealth_models


async def _iter_source(source: Any) -> AsyncIterator[dict[str, Any]]:
    """Yield models from a discovery source that may be a list, coroutine or iterator."""
    if hasattr(source, "__aiter__"):
        async for model in source:
            yield model
        return
    if inspect.isawaitable(source):
        source = await source
    for model in source or []:
        yield model


async def run_discovery(db_path: Path | None = None) -> dict[str, int]:
    """Run all discovery sources and upsert into registry. Returns counts per source."""
    import freeradar.registry as reg

    if db_path:
        reg.DB_PATH = db_path
        reg.JSON_PATH = db_path.parent / "free_models.json"

    init_db(db_path)
    conn = connect(db_path)

    counts = {src: 0 for src in DISCOVERY_SOURCES}
    seen: set[tuple[str, str]] = set()

    sources: dict[str, Any] = {
        "opencode": discover_from_opencode(),
        "openrouter": discover_from_openrouter(),
        "models_dev": discover_from_models_dev(),
        "github": discover_from_github(),
        "stealth_mentions": discover_stealth_mentions(),
    }

    try:
        for source_name, source_iter in sources.items():
            async for model in _iter_source(source_iter):
                provider = model["provider"]
                model_id = model["model_id"]
                key = (provider, model_id)

                if key in seen:
                    continue
                seen.add(key)

                action = upsert_model(conn, provider, model_id, model)
                log_discovery(conn, source_name, provider, model_id, action)
                counts[source_name] += 1

        # Mark missing models as removed
        removed = reg.mark_missing(conn, seen)
        counts["removed"] = removed

        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    return counts


def run_discovery_sync(db_path: Path | None = None) -> dict[str, int]:
    """Synchronous wrapper for discovery."""
    return asyncio.run(run_discovery(db_path))


if __name__ == "__main__":
    import sys
    result = run_discovery_sync()
    print(json.dumps(result, indent=2))
    sys.exit(0 if any(v > 0 for v in result.values()) else 1)