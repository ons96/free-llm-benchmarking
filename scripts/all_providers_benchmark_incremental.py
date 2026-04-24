import os
import json
import aiohttp
import asyncio
import csv
import time
from datetime import datetime

OPENCODE_CONFIG = "/home/osees/.config/opencode/opencode.json"
OUTPUT_FILE = "/home/osees/CodingProjects/testing/all_providers_benchmark.csv"
MAX_CONCURRENT = 3
MAX_TOKENS = 4000

SKIP_PATTERNS = [
    "embed", "rerank", "parse", "vision", "image", "audio",
    "whisper", "tts", "stt", "speech", "moderation", "safety",
    "search", "omni", "routing"
]

def load_models_from_config():
    with open(OPENCODE_CONFIG) as f:
        data = json.load(f)
    
    providers = data.get("provider", {})
    all_models = []
    
    for provider_name, info in providers.items():
        base_url = info.get("options", {}).get("baseURL", "")
        api_key = info.get("options", {}).get("apiKey", "")
        models = info.get("models", {})
        
        for model_name in models.keys():
            if not any(p in model_name.lower() for p in SKIP_PATTERNS):
                all_models.append({
                    "provider": provider_name,
                    "base_url": base_url or "",
                    "model": model_name,
                    "api_key": api_key or ""
                })
    
    return all_models

def get_api_key_for_provider(provider_name, model_info):
    env_key = os.environ.get("NVIDIA_API_KEY", "")
    if "nvidia" in provider_name.lower() and env_key:
        return env_key
    return model_info.get("api_key", "") or env_key

async def test_model(session, model_info):
    if not model_info.get("base_url"):
        return {**model_info, "ttft_s": None, "tps": None, "error": "NO_BASE_URL"}
    
    url = f"{model_info['base_url'].rstrip('/')}/chat/completions"
    api_key = get_api_key_for_provider(model_info["provider"], model_info)
    
    if not api_key:
        return {**model_info, "ttft_s": None, "tps": None, "error": "NO_API_KEY"}
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model_info["model"],
        "messages": [{"role": "user", "content": "Write Python factorial"}],
        "max_tokens": MAX_TOKENS,
        "stream": True
    }
    
    start = time.perf_counter()
    try:
        async with session.post(url, json=payload, headers=headers, timeout=25) as resp:
            if resp.status == 429:
                return {**model_info, "ttft_s": None, "tps": None, "error": "RATE_LIMIT"}
            if resp.status != 200:
                return {**model_info, "ttft_s": None, "tps": None, "error": f"HTTP_{resp.status}"}
            
            ttft = None
            tokens = 0
            
            async for line in resp.content:
                if line.strip().startswith(b"data:") and b"delta" in line:
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    tokens += 1
            
            total = time.perf_counter() - start
            
            if ttft and tokens > 0:
                tps = tokens / (total - ttft) if total > ttft else tokens / total
                return {
                    **model_info,
                    "ttft_s": round(ttft, 2),
                    "tps": round(tps, 1),
                    "tokens": tokens,
                    "error": None
                }
            return {**model_info, "ttft_s": None, "tps": None, "error": "NO_TOKENS"}
    
    except asyncio.TimeoutError:
        return {**model_info, "ttft_s": None, "tps": None, "error": "TIMEOUT"}
    except Exception as e:
        return {**model_info, "ttft_s": None, "tps": None, "error": str(e)[:40]}

def save_result(result):
    file_exists = os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0
    with open(OUTPUT_FILE, "a", newline="") as f:
        fieldnames = ["provider", "model", "base_url", "ttft_s", "tps", "tokens", "error", "timestamp"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        result["timestamp"] = datetime.now().isoformat()
        writer.writerow(result)

async def main():
    print("Multi-Provider Benchmark (Incremental Save)")
    print("=" * 60)
    
    models = load_models_from_config()
    print(f"Found {len(models)} models")
    
    # Check which models already have results
    completed_models = set()
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            reader = csv.DictReader(f)
            for row in reader:
                completed_models.add(f"{row['provider']}:{row['model']}")
        print(f"Already completed: {len(completed_models)} models")
    
    # Filter out already completed
    remaining = [m for m in models if f"{m['provider']}:{m['model']}" not in completed_models]
    print(f"Remaining: {len(remaining)} models")
    
    results = []
    errors = []
    
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        
        async def limited_test(m):
            async with semaphore:
                return await test_model(session, m)
        
        tasks = [limited_test(m) for m in remaining]
        
        completed = len(completed_models)
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            
            provider = result.get("provider", "?") or "?"
            model = result.get("model", "?") or "?"
            error = result.get("error", "") or ""
            
            # Save immediately
            save_result(result)
            
            if result.get("ttft_s"):
                print(f"[{completed}/{len(models)}] {provider[:12]:12s} {model[:35]:35s} TTFT={result['ttft_s']}s TPS={result['tps']}")
                results.append(result)
            else:
                print(f"[{completed}/{len(models)}] {provider[:12]:12s} {model[:35]:35s} ERR: {error[:15]}")
                errors.append(result)
            
            await asyncio.sleep(0.1)
    
    # Read all results and show top performers
    all_results = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("ttft_s") and row["ttft_s"] != "None" and row["ttft_s"] != "":
                    row["ttft_s"] = float(row["ttft_s"])
                    row["tps"] = float(row["tps"]) if row.get("tps") else 0
                    all_results.append(row)
    
    all_results.sort(key=lambda x: x.get("ttft_s", 999))
    
    print(f"\nResults: {OUTPUT_FILE}")
    print(f"Successful: {len(all_results)}/{len(models)}")
    
    if all_results:
        print("\nTOP 20 FASTEST TTFT:")
        for i, r in enumerate(all_results[:20], 1):
            print(f"{i:2d}. {r['provider'][:15]:15s}/{r['model'][:30]:30s}: TTFT={r['ttft_s']}s TPS={r['tps']}")
        
        all_results.sort(key=lambda x: x.get("tps", 0), reverse=True)
        print("\nTOP 20 HIGHEST TPS:")
        for i, r in enumerate(all_results[:20], 1):
            print(f"{i:2d}. {r['provider'][:15]:15s}/{r['model'][:30]:30s}: TPS={r['tps']} TTFT={r['ttft_s']}s")

if __name__ == "__main__":
    asyncio.run(main())
