#!/usr/bin/env python3
"""Generate top-10 summary markdown for GitHub Actions summary."""
import json
import sys

try:
    with open("data/summary.json") as f:
        data = json.load(f)
except Exception as e:
    print(f"Error: could not load data/summary.json: {e}", file=sys.stderr)
    sys.exit(1)

if not data:
    print("No benchmark data available.")
    sys.exit(0)

print(f"Found {len(data)} models. Showing top 10:")
for i, r in enumerate(data[:10], 1):
    provider = r.get("provider_name", "?")
    model = r.get("model_name", "?")
    ttft = r.get("avg_ttft_sec", "?")
    tps = r.get("avg_tps", "?")
    print(f"{i}. {provider}/{model}: TTFT={ttft}s TPS={tps}")
