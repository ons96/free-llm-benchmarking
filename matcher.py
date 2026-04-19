"""Normalize model names for matching against external benchmarks."""

import re
from difflib import SequenceMatcher

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

# Manual canonical map — edit when you spot a bad match
MANUAL_ALIASES = {
    # GPT-5 family
    "gpt-5": "gpt-5",
    "gpt-5-0806": "gpt-5",
    "gpt-5-2025-08-07": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5-mini-0806": "gpt-5-mini",
    "gpt-5-nano": "gpt-5-nano",
    "gpt-5-codex": "gpt-5-codex",
    "gpt-5.1": "gpt-5.1",
    "gpt-5.1-codex": "gpt-5.1-codex",
    # Claude
    "claude-opus-4": "claude-opus-4",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-sonnet-4": "claude-sonnet-4",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-haiku-4-5": "claude-haiku-4.5",
    # Gemini
    "gemini-3-pro": "gemini-3-pro",
    "gemini-3.5-pro": "gemini-3.5-pro",
    "gemini-3-flash": "gemini-3-flash",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
    # Grok
    "grok-4": "grok-4",
    "grok-4-fast": "grok-4-fast",
    "grok-code-fast-1": "grok-code-fast-1",
    # DeepSeek
    "deepseek-v3": "deepseek-v3",
    "deepseek-v3.1": "deepseek-v3.1",
    "deepseek-v3.2": "deepseek-v3.2",
    "deepseek-r1": "deepseek-r1",
    "deepseek-chat": "deepseek-v3",
    # GLM
    "glm-4.5": "glm-4.5",
    "glm-4.6": "glm-4.6",
    # Kimi
    "kimi-k2": "kimi-k2",
    "kimi-k2-0905": "kimi-k2",
    # Qwen
    "qwen3-coder": "qwen3-coder",
    "qwen3-max": "qwen3-max",
    "qwen3-next": "qwen3-next",
}


def normalize(name: str) -> str:
    """Normalize a model name for comparison."""
    n = name.lower().strip()

    # Strip provider prefixes
    for prefix in PROVIDER_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix) :]

    # Strip paths (e.g., "provider/path/model" → "model")
    if "/" in n:
        n = n.rsplit("/", 1)[-1]

    # Check manual alias table
    if n in MANUAL_ALIASES:
        return MANUAL_ALIASES[n]

    # Strip suffixes
    for pattern in SUFFIX_PATTERNS:
        n = re.sub(pattern, "", n)

    # Normalize separators
    n = n.replace("_", "-")
    # gpt5 → gpt-5
    n = re.sub(r"^gpt(\d)", r"gpt-\1", n)

    # Re-check aliases after normalization
    if n in MANUAL_ALIASES:
        return MANUAL_ALIASES[n]

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
