#!/usr/bin/env python3
import asyncio, aiohttp, time, os, json, csv
from datetime import datetime

KEY = os.environ.get('BLAZEAI_API_KEY', 'sk-blazeai')
ENDPOINT = 'https://blazeai.boxu.dev/api/chat/completions'
PROMPT = "Write Python factorial"  # streaming test
MAX_TOKENS = 4000
MAX_CONCURRENT = 5

MODELS = [
    "xinjianya/flux-schnell",
    "xinjianya/qwen3-30b-a3b",
    "xinjianya/qwen3-235b-a22b",
    "xinjianya/qwen3-14b",
    "xinjianya/qwen3-32b",
    "xinjianya/qwen3-2.5b",
    "xinjianya/qwen3-0.5b",
    "xinjianya/qwen3-1.7b",
    # comparison samples from existing OK results
    "blazeai/qwen/qwen3-coder-flash",
    "nvidia/nvidia/llama-3.1-nemotron-70b-instruct",
    "xinjianya/李/meta/llama-3.3-70b-instruct",
]

HEADERS = {'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}

async def test(session, model):
    payload = {'model': model, 'messages': [{'role': 'user', 'content': PROMPT}],
               'max_tokens': MAX_TOKENS, 'stream': True}
    start = time.perf_counter()
    ttft = None
    tokens = 0
    try:
        async with session.post(ENDPOINT, json=payload, headers=HEADERS, timeout=30) as r:
            if r.status != 200:
                txt = await r.text()
                return {'model': model, 'status': f'http_{r.status}', 'error': txt[:120]}
            async for line in r.content:
                if line.strip().startswith(b'data:') and b'delta' in line:
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    tokens += 1
            total = time.perf_counter() - start
            if ttft and tokens:
                tps = tokens / (total - ttft) if total > ttft else tokens / total
                return {'model': model, 'ttft_s': round(ttft, 3), 'tps': round(tps, 1),
                        'tokens': tokens, 'total_s': round(total, 3), 'status': 'ok'}
            return {'model': model, 'status': 'no_tokens'}
    except Exception as e:
        return {'model': model, 'status': 'error', 'error': str(e)[:120]}

async def main():
    results = []
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [test(session, m) for m in MODELS]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            r = await coro
            results.append(r)
            if r['status'] == 'ok':
                print(f"[{i+1}/{len(MODELS)}] ✓ {r['model'][:55]:<55s} TTFT={r['ttft_s']:.3f}s  TPS={r['tps']:.1f}  tokens={r['tokens']}")
            else:
                print(f"[{i+1}/{len(MODELS)}] ✗ {r['model'][:55]:<55s} {r['status']:>12s}  {r.get('error','')[:30]}")
    # summary
    ok = [r for r in results if r['status'] == 'ok']
    print(f"\nSUMMARY: {len(ok)}/{len(results)} succeeded")
    if ok:
        print("\nFASTEST (TPS):")
        for r in sorted(ok, key=lambda x: x['tps'], reverse=True)[:10]:
            print(f"  {r['tps']:>6.1f} tok/s  {r['ttft_s']:.3f}s TTFT  {r['model']}")
        print("\nLOWEST LATENCY (TTFT):")
        for r in sorted(ok, key=lambda x: x['ttft_s'])[:10]:
            print(f"  {r['ttft_s']:.3f}s TTFT  {r['tps']:>6.1f} tok/s  {r['model']}")
    # save
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = f'/home/osees/CodingProjects/llm-speedrun/data/xinjianya_benchmark_{ts}.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['model','status','ttft_s','tps','tokens','total_s','error'])
        w.writeheader()
        w.writerows(results)
    print(f"\nSaved: {out}")
    return results

if __name__ == '__main__':
    asyncio.run(main())
