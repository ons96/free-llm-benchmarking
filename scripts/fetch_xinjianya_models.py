import aiohttp, asyncio, json
from pathlib import Path

BASE = 'https://api.xinjianya.top/v1'
ENDPOINT = f'{BASE}/models'
KEY = os.environ.get('XINJIANYA_API_KEY', '')

async def main():
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get(ENDPOINT, headers={'Authorization': f'Bearer {KEY}'}) as r:
            data = await r.json()
            models = data.get('data', [])
            print(f"Total: {len(models)} models")
            print("\nModel IDs:\n" + "\n".join(f"- {m['id']}" for m in models))
            with open('data/xinjianya_real_models.json', 'w') as f:
                json.dump(data, f, indent=2)
            print("\nSaved → data/xinjianya_real_models.json")
    
asyncio.run(main())
