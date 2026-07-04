"""Normalize model names for matching against external benchmarks."""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

# Provider prefixes to strip
PROVIDER_PREFIXES = [
    "openai/",
    "anthropic/",
    "google/",
    "meta/",
    "mistralai/",
    "mistral/",
    "deepseek/",
    "qwen/",
    "alibaba/",
    "groq/",
    "fireworks/",
    "together/",
    "auto-",
    "kilocode/",
]

# Suffixes to strip (dates, versions, preview tags)
SUFFIX_PATTERNS = [
    r"-\d{8}$",  # -20250618
    r"-\d{4}-\d{2}-\d{2}$",  # -2025-06-18
    r"-\d{4}$",  # -0806
    r"-preview(-\d+)?$",
    r"-latest$",
    r":online$",
    r":free$",
    r":nitro$",
    r"-instruct$",
    r"-chat$",
]

# Shared alias map: config/model_alias_mapping.json is the single source of truth,
# shared with llm-leaderboard-aggregate. Loaded lazily and cached per-process.
# Fail-open: missing/malformed file -> empty map (heuristic-only normalization).
_ALIAS_MAP: dict[str, str] | None = None
_ALIAS_PATH = Path(__file__).resolve().parent / "config" / "model_alias_mapping.json"


def _load_aliases(path: Path | str | None = None, *, force: bool = False) -> dict[str, str]:
    global _ALIAS_MAP
    if _ALIAS_MAP is not None and not force:
        return _ALIAS_MAP
    p = Path(path) if path else _ALIAS_PATH
    mapping: dict[str, str] = {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ALIAS_MAP = mapping
        return mapping
    for g in raw.get("groups", []):
        canonical = g.get("canonical", "")
        if not canonical:
            continue
        norm_canonical = _normalize_no_alias(canonical)
        mapping.setdefault(norm_canonical, norm_canonical)
        for alias in g.get("aliases", []):
            na = _normalize_no_alias(alias)
            if na and na not in mapping:
                mapping[na] = norm_canonical
    _ALIAS_MAP = mapping
    return mapping


def _normalize_no_alias(name: str) -> str:
    """Heuristic normalization without alias-map lookup (used to build the map)."""
    n = name.lower().strip()
    for prefix in PROVIDER_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix):]
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    for pattern in SUFFIX_PATTERNS:
        n = re.sub(pattern, "", n)
    n = n.replace("_", "-")
    n = re.sub(r"^gpt(\d)", r"gpt-\1", n)
    return n


def normalize(name: str) -> str:
    """Normalize a model name for comparison."""
    n = _normalize_no_alias(name)
    aliases = _load_aliases()
    if n in aliases:
        return aliases[n]
    return n


def extract_reasoning_effort(name: str) -> tuple[str, str | None]:
    """Extract reasoning effort from benchmark names like 'GPT-5 (high)'."""
    m = re.search(r"[\(\[]\s*(low|medium|high|minimal)\s*[\)\]]", name, re.IGNORECASE)
    if m:
        effort = m.group(1).lower()
        cleaned = re.sub(
            r"\s*[\(\[]\s*(low|medium|high|minimal)\s*[\)\]]\s*",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned, effort
    return name, None


def similarity(a: str, b: str) -> float:
    """Return similarity ratio between two strings (0-1)."""
    return SequenceMatcher(None, a, b).ratio()


def match_model(
    target_canonical: str,
    candidates: list[str],
    threshold: float = 0.85,
) -> tuple[str | None, float, str]:
    """
    Find best-matching benchmark name for a target canonical name.
    Returns (matched_name, confidence, method).
    """
    # Exact canonical match
    for cand in candidates:
        if normalize(cand) == target_canonical:
            return cand, 1.0, "exact"

    # Fuzzy
    best_score = 0.0
    best_match = None
    for cand in candidates:
        cand_norm = normalize(cand)
        score = similarity(target_canonical, cand_norm)
        if score > best_score:
            best_score = score
            best_match = cand

    if best_score >= threshold:
        return best_match, best_score, "fuzzy"

    # Family fallback: common prefix match
    target_family = target_canonical.split("-")[0:2]
    if len(target_family) >= 2:
        family_prefix = "-".join(target_family)
        for cand in candidates:
            cand_norm = normalize(cand)
            if cand_norm.startswith(family_prefix):
                return cand, 0.7, "family"

    return None, best_score, "none"


if __name__ == "__main__":
    test_cases = [
        "gpt-5-2025-08-07",
        "openai/gpt-5",
        "claude-opus-4-5",
        "anthropic/claude-opus-4-20250514",
        "auto-grok-4",
        "gemini-3-pro-preview-11-2025",
        "deepseek-chat",
        "deepseek-v3.2-exp",
        "glm-4.6:online",
    ]
    for t in test_cases:
        print(f"{t:50} → {normalize(t)}")
