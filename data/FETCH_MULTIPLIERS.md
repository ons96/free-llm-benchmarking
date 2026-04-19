# How to Fetch Token Multipliers

Token multipliers are NOT available via API. They're shown on provider web dashboards.

## BlazeAI (blazeai.boxu.dev)

1. Open https://blazeai.boxu.dev/models in your browser
2. Open Developer Tools (F12)
3. Go to the Network tab
4. Refresh the page
5. Look for a request that returns model data with multipliers
6. Or use Console and run:
   ```js
   // Check if models are in React state
   Object.values(document.querySelectorAll('[data-model]')).forEach(el => {
     console.log(el.dataset.model, el.querySelector('.multiplier')?.textContent)
   })
   ```
7. Alternative: Look at the table cells - multipliers should be in a column

The models table is rendered client-side. You may need to:
- Look at the XHR/fetch requests in Network tab
- Find a request returning JSON with `multiplier` or `cost` fields

## Hapuppy (beta.hapuppy.com)

Requires Discord verification. Once verified:
1. Open https://beta.hapuppy.com/models
2. Same process as above

## What to Do With the Data

Once you have the multipliers, save them to:
`~/llm-speedrun/data/token_multipliers.json`

Format:
```json
[
  {"provider": "blazeai", "model": "gpt-5", "multiplier": 3.0},
  {"provider": "blazeai", "model": "gpt-5.1", "multiplier": 4.0}
]
```

Then update `multipliers.py` with the correct values.
