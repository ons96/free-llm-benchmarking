import os
import json
import aiohttp
import asyncio
import csv
import time
from datetime import datetime

OPENCODE_CONFIG = "/home/osees/.config/opencode/opencode.json"
OUTPUT_FILE = "/home/osees/CodingProjects/testing/all_providers_benchmark.csv"
MAX_CONCURRENT = 15
MAX_TOKENS = 4000
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
PROBE_TIMEOUT = 8
PROBE_TOKENS = 50

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

def load_completed():
    completed = set()
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                for row in csv.DictReader(f):
                    completed.add(f"{row['provider']}:{row['model']}")
        except Exception:
            pass
    return completed

file_lock = asyncio.Lock()

async def save_result(result):
    async with file_lock:
        file_exists = os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0
        with open(OUTPUT_FILE, "a", newline="") as f:
            fieldnames = ["provider", "model", "base_url", "ttft_s", "tps", "tokens", "error", "timestamp"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            result["timestamp"] = datetime.now().isoformat()
            writer.writerow(result)

async def probe_provider(session, provider_name, base_url, api_key):
    if not base_url:
        return "NO_BASE_URL"
    if not api_key:
        return "NO_API_KEY"

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "test",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False
    }

    try:
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT, sock_connect=CONNECT_TIMEOUT)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status in (200, 400, 404, 422):
                return "OK"
            if resp.status == 429:
                return "RATE_LIMIT_OK"
            return f"DEAD_{resp.status}"
    except (asyncio.TimeoutError, aiohttp.ClientConnectorError, OSError):
        return "CONN_FAIL"
    except Exception:
        return "OK"

async def test_model(session, model_info, timeout_cfg):
    if not model_info.get("base_url"):
        return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": "NO_BASE_URL"}

    url = f"{model_info['base_url'].rstrip('/')}/chat/completions"
    api_key = get_api_key_for_provider(model_info["provider"], model_info)

    if not api_key:
        return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": "NO_API_KEY"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model_info["model"],
        "messages": [{"role": "user", "content": "Write a Python function that computes the factorial of a number using recursion, with proper error handling for negative inputs."}],
        "max_tokens": MAX_TOKENS,
        "stream": True
    }

    start = time.perf_counter()
    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout_cfg) as resp:
            if resp.status == 429:
                return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": "RATE_LIMIT"}
            if resp.status != 200:
                return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": f"HTTP_{resp.status}"}

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
            return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": "NO_TOKENS"}

    except asyncio.TimeoutError:
        return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": "TIMEOUT"}
    except Exception as e:
        return {**model_info, "ttft_s": None, "tps": None, "tokens": None, "error": str(e)[:40]}

async def main():
    print("Multi-Provider Benchmark v2 (Fast)")
    print("=" * 60)

    models = load_models_from_config()
    print(f"Found {len(models)} models total")

    completed = load_completed()
    remaining = [m for m in models if f"{m['provider']}:{m['model']}" not in completed]
    print(f"Already completed: {len(completed)} | Remaining: {len(remaining)}")

    if not remaining:
        print("All models already tested!")
        return

    providers = {}
    for m in remaining:
        providers.setdefault(m["provider"], {
            "base_url": m["base_url"],
            "api_key": m.get("api_key", ""),
            "models": []
        })["models"].append(m)

    dead_providers = set()
    no_base_url_providers = set()

    print(f"\nPhase 1: Probing {len(providers)} providers...")
    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
    timeout_cfg = aiohttp.ClientTimeout(total=60, sock_connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout_cfg) as session:
        probe_tasks = {}
        for pname, pinfo in providers.items():
            if not pinfo["base_url"]:
                no_base_url_providers.add(pname)
                dead_providers.add(pname)
                continue
            api_key = get_api_key_for_provider(pname, {"api_key": pinfo["api_key"], "provider": pname})
            probe_tasks[pname] = asyncio.create_task(
                probe_provider(session, pname, pinfo["base_url"], api_key)
            )

        probe_results = await asyncio.gather(*probe_tasks.values(), return_exceptions=True)
        for pname, result in zip(probe_tasks.keys(), probe_results):
            if isinstance(result, Exception):
                status = f"ERROR: {result}"
            else:
                status = result
            model_count = len(providers[pname]["models"])
            if status == "CONN_FAIL":
                dead_providers.add(pname)
                print(f"  {pname}: DEAD (connection failed) - skipping {model_count} models")
            elif status.startswith("DEAD_"):
                dead_providers.add(pname)
                print(f"  {pname}: DEAD ({status}) - skipping {model_count} models")
            elif status == "NO_API_KEY":
                dead_providers.add(pname)
                print(f"  {pname}: NO_API_KEY - skipping {model_count} models")
            else:
                print(f"  {pname}: ALIVE ({status}) - testing {model_count} models")

    skipped = 0
    to_test = []
    for m in remaining:
        if m["provider"] in dead_providers:
            err = "CONN_FAIL" if m["provider"] not in no_base_url_providers else "NO_BASE_URL"
            if m["provider"] not in no_base_url_providers and f"{m['provider']}:{m['model']}" not in completed:
                r = {**m, "ttft_s": None, "tps": None, "tokens": None, "error": err}
                await save_result(r)
                skipped += 1
            else:
                skipped += 1
        else:
            to_test.append(m)

    print(f"\nSkipped {skipped} models on dead providers")
    print(f"Testing {len(to_test)} models on live providers")

    if not to_test:
        print("Nothing to test!")
        return

    print(f"\nPhase 2: Benchmarking {len(to_test)} models (concurrency={MAX_CONCURRENT})...")

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ttl_dns_cache=300)
    timeout_cfg = aiohttp.ClientTimeout(total=90, sock_connect=CONNECT_TIMEOUT, sock_read=30)

    success_count = 0
    error_count = 0

    async with aiohttp.ClientSession(connector=connector, timeout=timeout_cfg) as session:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)

        async def limited_test(m):
            async with semaphore:
                return await test_model(session, m, timeout_cfg)

        tasks = [limited_test(m) for m in to_test]

        done_count = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            done_count += 1

            await save_result(result)

            provider = result.get("provider", "?") or "?"
            model = result.get("model", "?") or "?"
            error = result.get("error", "") or ""

            if result.get("ttft_s"):
                success_count += 1
                print(f"[{done_count}/{len(to_test)}] {provider[:12]:12s} {model[:35]:35s} TTFT={result['ttft_s']}s TPS={result['tps']} tok={result.get('tokens', '?')}")
            else:
                error_count += 1
                print(f"[{done_count}/{len(to_test)}] {provider[:12]:12s} {model[:35]:35s} ERR: {error[:20]}")

    print(f"\n{'=' * 60}")
    print(f"Done! Tested {len(to_test)} models, {success_count} success, {error_count} errors")

    all_results = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            for row in csv.DictReader(f):
                if row.get("ttft_s") and row["ttft_s"] not in ("", "None"):
                    row["ttft_s"] = float(row["ttft_s"])
                    row["tps"] = float(row["tps"]) if row.get("tps") and row["tps"] not in ("", "None") else 0
                    all_results.append(row)

    all_results.sort(key=lambda x: x.get("ttft_s", 999))
    if all_results:
        print(f"\nTOP 20 FASTEST TTFT:")
        for i, r in enumerate(all_results[:20], 1):
            print(f"{i:2d}. {r['provider'][:15]:15s}/{r['model'][:30]:30s}: TTFT={r['ttft_s']}s TPS={r['tps']}")

        all_results.sort(key=lambda x: x.get("tps", 0), reverse=True)
        print(f"\nTOP 20 HIGHEST TPS:")
        for i, r in enumerate(all_results[:20], 1):
            print(f"{i:2d}. {r['provider'][:15]:15s}/{r['model'][:30]:30s}: TPS={r['tps']} TTFT={r['ttft_s']}s")

if __name__ == "__main__":
    asyncio.run(main())
