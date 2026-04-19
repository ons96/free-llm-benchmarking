"""Fetch and store token multipliers for API providers.

Most providers don't expose multipliers via API. They're typically shown on:
- Provider website model/pricing pages (rendered client-side)
- Dashboard after login

This module provides:
1. Known manual multiplier data (from manual scraping/research)
2. Scripts to scrape when possible
3. Storage format for multipliers DB
"""

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

MULTIPLIERS_FILE = Path(__file__).parent.parent / "data" / "token_multipliers.json"

# Manually researched multipliers (update as you discover them)
# Format: provider -> model_pattern -> multiplier
KNOWN_MULTIPLIERS = {
    "blazeai": {
        # These are typical multipliers for free-tier gateways
        # Actual values need to be scraped from blazeai.boxu.dev/models page
        # Placeholder values based on common patterns
        "deepseek-v3.*": 1.0,
        "glm-.*": 1.0,
        "gpt-5$": 3.0,
        "gpt-5\\.1": 4.0,
        "gpt-5-codex": 5.0,
        "grok-4.*": 2.0,
        "qwen.*": 1.0,
        "minimax.*": 1.0,
        "kimi.*": 1.5,
        "gemini.*pro": 2.5,
    },
    "hapuppy": {
        # Hapuppy requires Discord verification to access
        # Multipliers unknown - need manual research
        "gpt-5$": 3.0,
        "gpt-5\\.1": 4.0,
        "claude-opus-4": 15.0,
        "claude-sonnet-4": 3.0,
        "gemini.*pro": 2.5,
        "deepseek.*": 1.0,
        "qwen.*": 1.0,
        "glm.*": 1.0,
        "grok.*": 2.0,
    },
    # Kilo uses credit system, multipliers in their dashboard
    "kilocloud": {
        "claude-opus-4": 15.0,
        "claude-sonnet-4": 3.0,
        "gpt-5": 3.0,
        "gpt-5.1": 4.0,
        "gemini.*pro": 2.5,
        "deepseek.*": 0.5,  # DeepSeek is cheap
        "qwen.*": 0.5,
    },
}


@dataclass
class ModelMultiplier:
    provider: str
    model: str
    multiplier: float
    source: str  # "manual", "scraped", "api"
    notes: Optional[str] = None


def get_multiplier(provider: str, model: str) -> Optional[float]:
    """Get token multiplier for a provider/model combination."""
    import re

    provider_data = KNOWN_MULTIPLIERS.get(provider, {})
    for pattern, mult in provider_data.items():
        if re.match(pattern, model):
            return mult
    return None


def save_multipliers(multipliers: list[ModelMultiplier]) -> None:
    """Save multipliers to JSON file."""
    MULTIPLIERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "provider": m.provider,
            "model": m.model,
            "multiplier": m.multiplier,
            "source": m.source,
            "notes": m.notes,
        }
        for m in multipliers
    ]
    MULTIPLIERS_FILE.write_text(json.dumps(data, indent=2))


def load_multipliers() -> list[ModelMultiplier]:
    """Load multipliers from JSON file."""
    if not MULTIPLIERS_FILE.exists():
        return []
    data = json.loads(MULTIPLIERS_FILE.read_text())
    return [
        ModelMultiplier(
            provider=d["provider"],
            model=d["model"],
            multiplier=d["multiplier"],
            source=d["source"],
            notes=d.get("notes"),
        )
        for d in data
    ]


def fetch_blazeai_multipliers():
    """
    BlazeAI multipliers are shown on their /models page.
    The page is a React app that loads data client-side.
    The /api/models endpoint returns model info but not multipliers directly.

    To get multipliers:
    1. Open https://blazeai.boxu.dev/models in browser
    2. Open DevTools Console
    3. Run: JSON.stringify(__NEXT_DATA__.props.pageProps.models || [])
    4. Paste output into a file

    Or scrape the rendered HTML table after JS executes.
    """
    print("BlazeAI multipliers require manual scraping:")
    print("  1. Visit https://blazeai.boxu.dev/models")
    print("  2. Open DevTools console")
    print("  3. Run: copy(__NEXT_DATA__)")
    print("  4. Paste into ~/llm-speedrun/data/blazeai_next_data.json")
    print()
    print(
        "Then run: .venv/bin/python -c 'from multipliers import parse_blazeai_next_data; parse_blazeai_next_data()'"
    )


def parse_blazeai_next_data():
    """Parse BlazeAI __NEXT_DATA__ if available."""
    path = Path(__file__).parent.parent / "data" / "blazeai_next_data.json"
    if not path.exists():
        print(f"File not found: {path}")
        return []

    data = json.loads(path.read_text())
    # Navigate to models data - structure varies
    models = data.get("props", {}).get("pageProps", {}).get("models", [])

    results = []
    for m in models:
        model_id = m.get("id", m.get("name", ""))
        mult = m.get("multiplier", m.get("credit_multiplier", m.get("cost")))
        if model_id and mult:
            results.append(
                ModelMultiplier(
                    provider="blazeai",
                    model=model_id,
                    multiplier=float(mult),
                    source="scraped",
                )
            )

    if results:
        save_multipliers(results)
        print(f"Saved {len(results)} blazeai multipliers")
    return results


if __name__ == "__main__":
    print("=== Token Multipliers ===")
    print()
    print("To fetch multipliers:")
    print()
    fetch_blazeai_multipliers()
    print()
    print("Known multipliers (from manual research):")
    for provider, models in KNOWN_MULTIPLIERS.items():
        print(f"\n  {provider}:")
        for pattern, mult in sorted(models.items()):
            print(f"    {pattern}: {mult}x")
