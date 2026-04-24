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
        async with session.post(url, json=payload, headers=headers, timeout=60) as resp:
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

async def main():
    print("Multi-Provider Benchmark (Fixed)")
    print("=" * 60)
    
    models = load_models_from_config()
    print(f"Found {len(models)} models")
    
    results = []
    errors = []
    
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        
        async def limited_test(m):
            async with semaphore:
                return await test_model(session, m)
        
        tasks = [limited_test(m) for m in models]
        
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            
            provider = result.get("provider", "?") or "?"
            model = result.get("model", "?") or "?"
            error = result.get("error", "") or ""
            
            if result.get("ttft_s"):
                print(f"[{completed}/{len(models)}] {provider[:12]:12s} {model[:35]:35s} TTFT={result['ttft_s']}s TPS={result['tps']}")
                results.append(result)
            else:
                print(f"[{completed}/{len(models)}] {provider[:12]:12s} {model[:35]:35s} ERR: {error[:15]}")
                errors.append(result)
            
            await asyncio.sleep(0.15)
    
    results.sort(key=lambda x: x.get("ttft_s") or 999)
    
    with open(OUTPUT_FILE, "w", newline="") as f:
        fieldnames = ["provider", "model", "base_url", "ttft_s", "tps", "tokens", "error", "timestamp"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results + errors:
            r["timestamp"] = datetime.now().isoformat()
            writer.writerow(r)
    
    print(f"\nResults: {OUTPUT_FILE}")
    print(f"Successful: {len(results)}/{len(models)}")
    print(f"Errors: {len(errors)}")
    
    if results:
        print("\nTOP 15 FASTEST:")
        for i, r in enumerate(results[:15], 1):
            print(f"{i:2d}. {r['provider']}/{r['model']}: TTFT={r['ttft_s']}s TPS={r['tps']}")

if __name__ == "__main__":
    asyncio.run(main())