#!/usr/bin/env python3
"""Build leaderboard.json for GitHub Pages site."""
import csv, json

# Load leaderboard.csv and convert to clean JSON
with open("data/leaderboard.csv") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

cleaned = []
for r in rows:
    try:
        tokens = float(r.get("avg_tokens", "0") or 0
        tps = float(r.get("TPS", "0") or 0
        ttft = float(r.get("TTFT_sec", "0") or 0
        total = float(r.get("10K_total_sec", "0") or 0
        
        # Flag suspicious data
        flags = []
        suspicious = False
        
        if tokens < 50:
            suspicious = True
            flags.append("low_tokens")
        if tps > 50000 and tokens < 100:
            flags.append("inflated_tps")
        if not r.get("effort"):
            r["effort"] = "none"  # Fix blank effort values
            
        cleaned.append({
            "rank": int(r["rank"]) if r["rank"].isdigit() else 0,
            "provider_name": r.get("provider", ""),
            "model_name": r.get("model", ""),
            "reasoning_effort": r.get("effort", "none"),
            "avg_ttft_sec": ttft,
            "avg_tps": tps,
            "avg_output_tokens": tokens,
            "est_10k_total_s": total,
            "flags": flags,
            "is_suspicious": suspicious,
        })
    except Exception as e:
        print(f"Error processing row: {e}")

# Sort by 10K time
cleaned.sort(key=lambda x: x["est_10k_total_s"])

# Reassign ranks
for i, r in enumerate(cleaned, 1):
    r["rank"] = i

# Save JSON
with open("docs/leaderboard.json", "w") as f:
    json.dump(cleaned, f, indent=2)

print(f"Saved {len(cleaned)} rows to docs/leaderboard.json")
suspicious = sum(1 for r in cleaned if r["is_suspicious"])
print(f"Suspicious (low tokens): {suspicious} rows")
