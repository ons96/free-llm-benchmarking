import csv

AVG_TOKENS_PER_CALL = 4000
INPUT_FILE = "/home/osees/CodingProjects/testing/all_providers_benchmark.csv"
OUTPUT_FILE = "/home/osees/CodingProjects/testing/all_providers_benchmark_with_estimates.csv"

results = []
with open(INPUT_FILE) as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row.get("ttft_s") and row["ttft_s"] not in ("", "None"):
            ttft = float(row["ttft_s"])
            tps = float(row["tps"]) if row.get("tps") else 0
            if tps > 0:
                estimated_total_time = round(ttft + (AVG_TOKENS_PER_CALL / tps), 2)
            else:
                estimated_total_time = None
        else:
            estimated_total_time = None
        
        row["estimated_total_time_s"] = estimated_total_time if estimated_total_time else ""
        results.append(row)

with open(OUTPUT_FILE, "w", newline="") as f:
    fieldnames = ["provider", "model", "base_url", "ttft_s", "tps", "tokens", "estimated_total_time_s", "error", "timestamp"]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for r in results:
        writer.writerow(r)

print(f"Added estimated_total_time_s column (based on {AVG_TOKENS_PER_CALL} tokens per call)")
print(f"Output: {OUTPUT_FILE}")

valid_results = [r for r in results if r.get("estimated_total_time_s")]
valid_results.sort(key=lambda x: float(x["estimated_total_time_s"]))

print(f"\nTOP 20 FASTEST TOTAL COMPLETION TIME:")
for i, r in enumerate(valid_results[:20], 1):
    print(f"{i:2d}. {r['provider'][:15]:15s}/{r['model'][:30]:30s}: {r['estimated_total_time_s']}s (TTFT={r['ttft_s']}s, TPS={r['tps']})")
