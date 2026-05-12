#!/usr/bin/env python3
"""Build leaderboard.json for GitHub Pages site."""
import csv
import json
from pathlib import Path

INFLATED_TPS_PATTERNS = [
    "content-safety",
    "topic-control",
    "safety-guard",
    "safety-reasoning",
    "translation",
    "calibration",
]

INFLATED_TPS_PROVIDERS = {
    "kilo",
    "opencode",
}


def _is_suspicious_tps(provider: str, model: str, tps: float, tokens: float) -> tuple[bool, list[str]]:
    flags = []
    suspicious = False

    if tokens < 50:
        suspicious = True
        flags.append("low_tokens")
    elif tokens < 200:
        flags.append("low_tokens_caution")

    model_lower = model.lower()
    for pat in INFLATED_TPS_PATTERNS:
        if pat.lower() in model_lower:
            suspicious = True
            flags.append(f"content_filter_model")
            break

    if provider.lower() in INFLATED_TPS_PROVIDERS and tokens < 600:
        suspicious = True
        flags.append("proxy_gateway_short_response")

    if tps > 50000 and tokens < 100:
        flags.append("inflated_tps")
        suspicious = True
    elif tps > 2000 and tokens < 500:
        flags.append("potentially_inflated_tps")
        suspicious = True

    if tps > 100000:
        flags.append("fake_tps")
        suspicious = True

    return suspicious, flags


with open("data/leaderboard.csv") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

cleaned = []
for r in rows:
    try:
        tokens = float(r.get("avg_tokens", "0") or 0)
        tps = float(r.get("TPS", "0") or 0)
        ttft = float(r.get("TTFT_sec", "0") or 0)
        total = float(r.get("10K_total_sec", "0") or 0)
        provider = r.get("provider", "")
        model = r.get("model", "")

        suspicious, flags = _is_suspicious_tps(provider, model, tps, tokens)

        effort = r.get("effort", "") or "none"

        cleaned.append({
            "rank": int(r["rank"]) if r["rank"].isdigit() else 0,
            "provider_name": provider,
            "model_name": model,
            "reasoning_effort": effort,
            "avg_ttft_sec": ttft,
            "avg_tps": tps,
            "avg_output_tokens": tokens,
            "est_10k_total_s": total,
            "flags": flags,
            "is_suspicious": suspicious,
        })
    except Exception as e:
        print(f"Error processing row: {e}")

cleaned.sort(key=lambda x: x["est_10k_total_s"] if x["est_10k_total_s"] > 0 else float("inf"))

for i, r in enumerate(cleaned, 1):
    r["rank"] = i

docs_data = Path("docs/data")
docs_data.mkdir(parents=True, exist_ok=True)

with open("docs/data/leaderboard.json", "w") as f:
    json.dump(cleaned, f, indent=2)

with open("docs/leaderboard.json", "w") as f:
    json.dump(cleaned, f, indent=2)

print(f"Saved {len(cleaned)} rows to docs/data/leaderboard.json")
suspicious = sum(1 for r in cleaned if r["is_suspicious"])
fake_tps = sum(1 for r in cleaned if "fake_tps" in r["flags"])
content_filter = sum(1 for r in cleaned if "content_filter_model" in r["flags"])
print(f"Suspicious rows: {suspicious} (fake_tps={fake_tps}, content_filter={content_filter})")
