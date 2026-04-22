"""Parse opencode.json and gateway configs into test targets."""

import json
import re
from typing import Any


def _parse_jsonc(text: str) -> Any:
    """Parse JSONC (strip comments first)."""
    # Strip single-line comments (//) only when preceded by whitespace or {
    text = re.sub(r"(^|[{}\s])//.*$", r"\1", text, flags=re.MULTILINE)
    # Strip multi-line comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return json.loads(text)


from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Use .jsonc for the full provider set
OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.jsonc"
GATEWAY_VIRTUAL = Path.home() / "LLM-API-Key-Proxy" / "config" / "virtual_models.yaml"

# Providers to skip by default
SKIP_PROVIDERS = {"cursor-proxy", "ktai-paid", "wiwi", "supacoder"}
# Paid/credits providers (opt-in with --include-credits)
CREDITS_PROVIDERS = {"ktai-paid"}

# Model patterns considered expensive (high token multipliers)
EXPENSIVE_PATTERNS = [
    r"gpt-5-pro",
    r"gpt-5\.1-pro",
    r"grok-4-heavy",
    r"claude-opus-4-5",
    r"claude-sonnet-4\.5",
    r"o1-pro",
    r"o3-pro",
]

# Model families that support reasoning_effort (matches anywhere in name)
REASONING_FAMILIES = [
    # OpenAI GPT-5 family
    r"gpt-5",
    r"gpt5",
    r"\bo[134]\b",
    r"\bo3\b",
    r"\bo4\b",
    # Grok
    r"grok-4",
    r"grok4",
    r"grok-4\.1",
    # Gemini 3.x pro variants
    r"gemini-3[\.\d]*-pro",
    r"gemini-3\.1-pro",
    r"gemini-3-pro",
    r"gemini-3\.1-pro",
    # DeepSeek reasoning
    r"deepseek-r",
    r"deepseek-r1",
    # Qwen thinking variants
    r"qwen.*thinking",
    r"qwen3.*thinking",
    r"qwen-qwen3\.5.*thinking",
    r"qwen-qwen3\.6.*thinking",
    # Claude 4.x
    r"claude-opus-4",
    r"claude-sonnet-4",
    r"claude-haiku-4",
    r"claude-4-opus",
    r"claude-4-sonnet",
    r"claude-4-haiku",
    # GLM thinking variants
    r"glm-4\.6",
    r"glm-4\.5",
    r"glm-5",
    r"glm.*think",
    r"glm-5\.1-think",
    # Kimi K2.x (reasoning)
    r"kimi-k2",
    r"kimi-k2\.5",
]


@dataclass
class Target:
    provider_name: str
    base_url: str
    api_key: str
    model_name: str
    source: str = "direct"  # "direct" or "gateway"
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


def load_opencode_targets(
    include_credits: bool = False,
    include_expensive: bool = False,
    provider_filter: Optional[str] = None,
    model_filter: Optional[str] = None,
) -> list[Target]:
    """Parse opencode.json providers into Target list."""
    data = _parse_jsonc(OPENCODE_JSON.read_text())
    providers = data.get("provider", {})
    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()

    for pname, pconfig in providers.items():
        # Skip logic
        if pname in SKIP_PROVIDERS and pname not in CREDITS_PROVIDERS:
            continue
        if pname in CREDITS_PROVIDERS and not include_credits:
            continue
        if provider_filter and not _glob_match(pname, provider_filter):
            continue

        base_url = pconfig.get("baseURL", "")
        api_key = pconfig.get("apiKey", "")

        # opencode.json nests these under options
        options = pconfig.get("options", {})
        if not base_url and options:
            base_url = options.get("baseURL", "")
        if not api_key and options:
            api_key = options.get("apiKey", "")

        models = pconfig.get("models", {})
        for mname, mconfig in models.items():
            if model_filter and not _glob_match(mname, model_filter):
                continue

            expensive = _is_expensive(mname)
            if expensive and not include_expensive:
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
                    model_name=mname,
                    source="direct",
                    supports_reasoning=_supports_reasoning(mname),
                    is_expensive=expensive,
                )
            )

    return targets


def load_gateway_targets() -> list[Target]:
    """Create targets for gateway virtual models."""
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

    # Gateway base URL from opencode.json "custom" provider
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
                supports_reasoning=False,  # gateway handles routing
                is_expensive=False,
            )
        )

    return targets


def load_all_targets(
    include_credits: bool = False,
    include_expensive: bool = False,
    provider_filter: Optional[str] = None,
    model_filter: Optional[str] = None,
    include_gateway: bool = True,
) -> list[Target]:
    """Load targets from both opencode.json and gateway."""
    targets = load_opencode_targets(
        include_credits=include_credits,
        include_expensive=include_expensive,
        provider_filter=provider_filter,
        model_filter=model_filter,
    )
    if include_gateway and not provider_filter:
        targets.extend(load_gateway_targets())
    elif provider_filter and _glob_match("gateway", provider_filter):
        targets.extend(load_gateway_targets())
    return targets


def _glob_match(name: str, pattern: str) -> bool:
    """Simple glob matching (supports * wildcard)."""
    import fnmatch

    return fnmatch.fnmatch(name.lower(), pattern.lower())


if __name__ == "__main__":
    targets = load_all_targets()
    by_provider: dict[str, int] = {}
    for t in targets:
        by_provider[t.provider_name] = by_provider.get(t.provider_name, 0) + 1
    print(f"Total targets: {len(targets)}")
    for p, c in sorted(by_provider.items()):
        print(f"  {p}: {c} models")
    reasoning = [t for t in targets if t.supports_reasoning]
    print(f"Reasoning-capable: {len(reasoning)}")
    expensive = [t for t in targets if t.is_expensive]
    print(f"Expensive (skipped by default): {len(expensive)}")
