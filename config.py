"""Parse opencode.json and gateway configs into test targets."""

import json
import os
import re
from typing import Any


def _parse_jsonc(text: str) -> Any:
    """Parse JSONC (strip comments first)."""
    text = re.sub(r"(^|[{}\s])//.*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return json.loads(text)


from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.json"
if not OPENCODE_JSON.exists():
    OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.jsonc"
GATEWAY_VIRTUAL = Path.home() / "LLM-API-Key-Proxy" / "config" / "virtual_models.yaml"

SKIP_PROVIDERS = {
    "cliproxyapi",  # dead
    "supacoder",   # dead (no live models)
    "zenllm",      # 401 auth
    "swiftrouter", # 401 auth
    "zenmux",      # 401 auth
    "llmgateway",  # 401 auth
    "kilocloud",   # dead (all rate limited)
}

FINITE_CREDIT_PROVIDERS = {"ktai-paid"}

NEW_API_PROVIDERS = {
    "xinjianya",
    "huashang",
    "lotte-library",
}

DAILY_QUOTA_PROVIDERS = {
    "nvidia",
}

FREE_CREDIT_PROVIDERS = {
    "hapuppy", "blazeai", "nvidia",
    "aitools", "bluesminds", "claude-carter",
    "ktai", "logfare", "swiftrouter",
    "zenllm",
    "supacoder", "ollama-cloud", "wiwi",
    "kilo",
    "cursor-proxy",
    "ktai-paid",
    "freetheai",
    "aihubmix",
    "cortecs",
    "opencode",
    "github-models",
    "iflowcn",
    "zhipuai-coding-plan",
    "zai-coding-plan",
    "alibaba-coding-plan-cn",
    "alibaba-coding-plan",
    "tencent-coding-plan",
    "llama",
    "modelscope",
    "minimax-coding-plan",
    "minimax-cn-coding-plan",
    "poe",
    "groq",
    "huggingface",
    "siliconflow-cn",
}

NO_STREAM_PROVIDERS = {"bluesminds"}

FREE_MODEL_PATTERNS = ["free", "big-pickle"]

STREAM_MODEL_OVERRIDES = set()

RATE_LIMITS = {
    "supacoder": 7,
    "hapuppy": 30,
    "blazeai": 20,
    "ollama-cloud": 10,
    "xinjianya": 10,
    "opencodezen": 60,
    "aihubmix": 20,
    "cortecs": 10,
    "opencode": 30,
    "ktai": 15,
    "wiwi": 20,
    "github-models": 30,
    "iflowcn": 20,
    "freetheai": 20,
    "zhipuai-coding-plan": 20,
    "zai-coding-plan": 20,
    "alibaba-coding-plan": 15,
    "alibaba-coding-plan-cn": 15,
    "tencent-coding-plan": 15,
    "llama": 20,
    "modelscope": 15,
    "minimax-coding-plan": 15,
    "poe": 20,
    "groq": 30,
    "huggingface": 15,
    "siliconflow-cn": 15,
    "lotte-library": 20,
    "huashang": 20,
    "10dian-ai": 20,
}

EXPENSIVE_PATTERNS = [
    r"gpt-5-pro",
    r"gpt-5\.1-pro",
    r"grok-4-heavy",
    r"claude-opus-4-5",
    r"claude-sonnet-4\.5",
    r"o1-pro",
    r"o3-pro",
]

REASONING_FAMILIES = [
    r"gpt-5",
    r"gpt5",
    r"\bo[134]\b",
    r"\bo3\b",
    r"\bo4\b",
    r"grok-4",
    r"grok4",
    r"grok-4\.1",
    r"gemini-3[\.\d]*-pro",
    r"gemini-3\.1-pro",
    r"gemini-3-pro",
    r"gemini-3\.1-pro",
    r"deepseek-r",
    r"deepseek-r1",
    r"qwen.*thinking",
    r"qwen3.*thinking",
    r"qwen-qwen3\.5.*thinking",
    r"qwen-qwen3\.6.*thinking",
    r"claude-opus-4",
    r"claude-sonnet-4",
    r"claude-haiku-4",
    r"claude-4-opus",
    r"claude-4-sonnet",
    r"claude-4-haiku",
    r"glm-4\.6",
    r"glm-4\.5",
    r"glm[-]?4\.7",
    r"glm-5",
    r"glm.*think",
    r"glm-5\.1-think",
    r"nemotron",
    r"kimi-k2",
    r"kimi-k2\.5",
]


@dataclass
class Target:
    provider_name: str
    base_url: str
    api_key: str
    model_name: str
    source: str = "direct"
    supports_reasoning: bool = False
    is_expensive: bool = False

    def __repr__(self) -> str:
        return f"Target({self.provider_name}/{self.model_name})"


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(re.search(p, name.lower()) for p in patterns)


def _is_expensive(model: str) -> bool:
    return _matches_any(model, EXPENSIVE_PATTERNS)


def _supports_reasoning(model: str) -> bool:
    return _matches_any(model, REASONING_FAMILIES)


def _resolve_env_var(s: str) -> str:
    if not s:
        return s
    return os.path.expandvars(s)


def is_model_free(provider_name: str, model_name: str) -> bool:
    if provider_name in FREE_CREDIT_PROVIDERS:
        return True
    if "kilo" in provider_name.lower():
        return "free" in model_name.lower()
    name_lower = model_name.lower()
    return any(pat in name_lower for pat in FREE_MODEL_PATTERNS)


def fetch_model_pricing(base_url: str, api_key: str) -> dict[str, dict]:
    import httpx

    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        result = resp.json()
        models = result.get("data", result) if isinstance(result, dict) else result
        pricing = {}
        for m in models:
            mid = m.get("id", "")
            p = {}
            pr = m.get("pricing", {})
            if pr:
                inp = pr.get("prompt", pr.get("input", "0"))
                out = pr.get("completion", pr.get("output", "0"))
                try:
                    p["input"] = float(inp)
                    p["output"] = float(out)
                except (ValueError, TypeError):
                    pass
            for field in ["input_price", "output_price", "input_cost", "output_cost"]:
                if field in m:
                    key = "input" if "input" in field else "output"
                    try:
                        p[key] = float(m[field])
                    except (ValueError, TypeError):
                        pass
            if p:
                pricing[mid] = p
        return pricing
    except Exception:
        return {}


def fetch_newapi_pricing(base_url: str, api_key: str) -> dict[str, dict]:
    import httpx
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        resp = httpx.get(
            f"{base}/api/pricing",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        result = resp.json()
        if not result.get("success"):
            return {}
        pricing = {}
        for m in result.get("data", []):
            model_name = m.get("model_name", "")
            if not model_name:
                continue
            pricing[model_name] = {
                "ratio": m.get("model_ratio", 1),
                "groups": m.get("enable_groups", []),
                "completion_ratio": m.get("completion_ratio", 1),
            }
        return pricing
    except Exception:
        return {}


def _is_localhost(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url


def _probe_provider(base_url: str, api_key: str) -> bool:
    import httpx
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def load_opencode_targets(
    include_expensive: bool = False,
    include_paid: bool = False,
    provider_filter: Optional[str] = None,
    model_filter: Optional[str] = None,
    skip_offline: bool = True,
) -> list[Target]:
    data = _parse_jsonc(OPENCODE_JSON.read_text())
    providers = data.get("provider", {})
    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()
    pricing_cache: dict[str, dict] = {}
    newapi_cache: dict[str, dict] = {}
    probed_offline: set[str] = set()

    for pname, pconfig in providers.items():
        if pname in SKIP_PROVIDERS and pname not in FREE_CREDIT_PROVIDERS:
            continue
        if provider_filter and not _glob_match(pname, provider_filter):
            continue

        base_url = pconfig.get("baseURL", "")
        api_key = pconfig.get("apiKey", "")

        options = pconfig.get("options", {})
        if not base_url and options:
            base_url = options.get("baseURL", "")
        if not api_key and options:
            api_key = options.get("apiKey", "")

        api_key = _resolve_env_var(api_key)

        if skip_offline and _is_localhost(base_url) and not _probe_provider(base_url, api_key):
            probed_offline.add(pname)
            continue

        if (
            not include_paid
            and pname not in FREE_CREDIT_PROVIDERS
            and pname not in pricing_cache
        ):
            pricing_cache[pname] = fetch_model_pricing(base_url, api_key)

        if not include_paid and pname in NEW_API_PROVIDERS and pname not in newapi_cache:
            newapi_cache[pname] = fetch_newapi_pricing(base_url, api_key)

        models = pconfig.get("models", {})
        for mname, mconfig in models.items():
            if model_filter and not _glob_match(mname, model_filter):
                continue

            model_id = mname
            display_name = mconfig.get("name") if isinstance(mconfig, dict) else None
            if display_name and "/" in display_name:
                model_name = display_name
            else:
                model_name = model_id

            if not include_paid and pname not in FREE_CREDIT_PROVIDERS:
                is_free = is_model_free(pname, model_name)
                if not is_free:
                    if pname in NEW_API_PROVIDERS:
                        np = newapi_cache.get(pname, {})
                        nmeta = np.get(model_name, {})
                        ratio = nmeta.get("ratio", 999)
                        groups = nmeta.get("groups", [])
                        if ratio != 0 or ("default" not in groups and "Free" not in groups):
                            continue
                    else:
                        p = pricing_cache.get(pname, {})
                        model_pricing = p.get(model_name, {})
                        if model_pricing:
                            inp = model_pricing.get("input", 0)
                            out = model_pricing.get("output", 0)
                            if inp > 0 or out > 0:
                                continue
                        else:
                            continue

            if "kilo" in pname.lower() and "free" not in model_name.lower():
                continue

            expensive = _is_expensive(mname)
            force_skip_expensive = pname in FINITE_CREDIT_PROVIDERS and not include_expensive
            if (expensive and not include_expensive) or force_skip_expensive:
                continue

            key = (pname, mname)
            if key in seen:
                continue
            seen.add(key)

            targets.append(
                Target(
                    provider_name=pname,
                    base_url=base_url,
                    api_key=api_key,
                    model_name=model_name,
                    source="direct",
                    supports_reasoning=_supports_reasoning(mname),
                    is_expensive=expensive,
                )
            )

    if probed_offline:
        print(f"Skipped offline localhost providers: {', '.join(sorted(probed_offline))}")

    return targets


def load_gateway_targets() -> list[Target]:
    try:
        import yaml
    except ImportError:
        print("pyyaml not installed, skipping gateway targets")
        return []

    if not GATEWAY_VIRTUAL.exists():
        return []

    data = yaml.safe_load(GATEWAY_VIRTUAL.read_text())
    if not data or "virtual_models" not in data:
        return []

    oc = _parse_jsonc(OPENCODE_JSON.read_text())
    custom = oc.get("provider", {}).get("custom", {})
    base_url = custom.get("baseURL", "http://40.233.101.233:8000/v1")
    api_key = custom.get("apiKey", "")

    targets: list[Target] = []
    vms = data["virtual_models"]
    if isinstance(vms, dict):
        vm_names = list(vms.keys())
    else:
        vm_names = [v.get("name", "") if isinstance(v, dict) else str(v) for v in vms]

    for vm_name in vm_names:
        if not vm_name:
            continue
        targets.append(
            Target(
                provider_name="gateway",
                base_url=base_url,
                api_key=api_key,
                model_name=vm_name,
                source="gateway",
                supports_reasoning=False,
                is_expensive=False,
            )
        )

    return targets


def load_all_targets(
    include_expensive: bool = False,
    include_paid: bool = False,
    provider_filter: Optional[str] = None,
    model_filter: Optional[str] = None,
    include_gateway: bool = True,
    skip_offline: bool = True,
) -> list[Target]:
    targets = load_opencode_targets(
        include_expensive=include_expensive,
        include_paid=include_paid,
        provider_filter=provider_filter,
        model_filter=model_filter,
        skip_offline=skip_offline,
    )
    if include_gateway and not provider_filter:
        targets.extend(load_gateway_targets())
    elif provider_filter and _glob_match("gateway", provider_filter):
        targets.extend(load_gateway_targets())
    return targets


def get_provider_count(targets: list[Target]) -> int:
    return len({t.provider_name for t in targets})


def _glob_match(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(name.lower(), pattern.lower())


if __name__ == "__main__":
    targets = load_all_targets()
    by_provider: dict[str, int] = {}
    for t in targets:
        by_provider[t.provider_name] = by_provider.get(t.provider_name, 0) + 1
    print(f"Total targets: {len(targets)}")
    for p, c in sorted(by_provider.items()):
        print(f" {p}: {c} models")
    reasoning = [t for t in targets if t.supports_reasoning]
    print(f"Reasoning-capable: {len(reasoning)}")
    expensive = [t for t in targets if t.is_expensive]
    print(f"Expensive (skipped by default): {len(expensive)}")