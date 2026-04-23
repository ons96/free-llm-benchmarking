# Plan: Free-Only Model Filtering with Auto Pricing Detection

## Goal
Only test 100% free LLMs by default. Paid models should only be tested if their
provider is in a whitelist of "free recurring credits" providers.

## Background
- Project: `/home/owen/llm-speedrun/` (fork of ons96/free-llm-speedrun)
- Config: `/home/owen/.config/opencode/opencode.jsonc` (JSONC) and `opencode.json` (JSON backup)
- Main files: `config.py` (target loading), `runner.py` (test runner), `db.py` (SQLite), `cli.py` (CLI)
- 508 total targets currently, many are paid models on kilocloud (95), hapuppy (71), etc.

## Key Rules (from user)
1. **Only test 100% free LLMs by default** — $0 input + $0 output token pricing
2. **Providers with free recurring credits are OK** — hapuppy, blazeai (user confirmed these)
3. **kilo/kilocloud** — only models with "free" in the name (existing rule in config.py already filters these partially)
4. **opencodezen** — only models with "free" in the name OR `big-pickle` (which is free)
5. **Auto-detect pricing** — when adding new providers/models, query the `/v1/models` API endpoint
   to check pricing. If the model has pricing fields and they're >$0, skip it unless the
   provider is in the free-credits whitelist.

---

## Step 1: Add `FREE_CREDIT_PROVIDERS` set to `config.py`

After the existing `CREDITS_PROVIDERS` (line 35), add:

```python
# Providers where paid models are OK to test (user has free recurring credits/quota)
FREE_CREDIT_PROVIDERS = {"hapuppy", "blazeai", "ollama-cloud"}
```

These providers' models are always tested regardless of pricing.

## Step 2: Add `FREE_MODEL_PATTERNS` to `config.py`

After `FREE_CREDIT_PROVIDERS`, add:

```python
# Model name patterns that indicate a free model (case-insensitive substring match)
FREE_MODEL_PATTERNS = ["free", "big-pickle"]
```

Any model whose name contains one of these strings is treated as free.

## Step 3: Add `is_model_free()` function to `config.py`

```python
def is_model_free(provider_name: str, model_name: str) -> bool:
    """Check if a model should be tested for free (no cost to user)."""
    # Providers with free credits — all their models are OK
    if provider_name in FREE_CREDIT_PROVIDERS:
        return True
    # Models with 'free' in name or other known-free patterns
    name_lower = model_name.lower()
    return any(pat in name_lower for pat in FREE_MODEL_PATTERNS)
```

## Step 4: Add pricing query function to `config.py`

```python
import httpx

def fetch_model_pricing(base_url: str, api_key: str) -> dict[str, dict]:
    """Query /v1/models endpoint and extract pricing info.
    
    Returns: {model_id: {"input": float, "output": float}} where prices are per-token.
    Models with no pricing data or $0 pricing are considered free.
    """
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        result = resp.json()
        models = result.get("data", result) if isinstance(result, dict) else result
        pricing = {}
        for m in models:
            mid = m.get("id", "")
            p = {}
            # OpenRouter-style: m["pricing"]["prompt"]"] and m["pricing"]["completion"]
            pr = m.get("pricing", {})
            if pr:
                # Values are strings like "0.00000003" per token
                inp = pr.get("prompt", pr.get("input", "0"))
                out = pr.get("completion", pr.get("output", "0"))
                try:
                    p["input"] = float(inp)
                    p["output"] = float(out)
                except (ValueError, TypeError):
                    pass
            # Some providers use top-level fields
            for field in ["input_price", "output_price", "input_cost", "output_cost"]:
                if field in m:
                    key = "input" if "input" in field else "output"
                    try:
                        p[key] = float(m[field])
                    except (ValueError, TypeError):
                        pass
            if p:
                pricing[mid] = p
        return pricing
    except Exception:
        return {}
```

## Step 5: Integrate pricing check into `load_opencode_targets()` in `config.py`

Modify the function to check pricing for non-whitelisted providers. The logic:

```python
def load_opencode_targets(...) -> list[Target]:
    data = _parse_jsonc(OPENCODE_JSON.read_text())
    providers = data.get("provider", {})
    targets: list[Target] = []
    seen: set[tuple[str, str]] = set()
    
    # Cache pricing per provider to avoid repeated queries
    pricing_cache: dict[str, dict] = {}

    for pname, pconfig in providers.items():
        # ... existing skip logic ...
        
        base_url = pconfig.get("baseURL", "")
        api_key = pconfig.get("apiKey", "")
        # ... existing base_url/api_key fallback logic ...
        
        # Fetch pricing for this provider if not in free-credits whitelist
        if pname not in FREE_CREDIT_PROVIDERS and pname not in pricing_cache:
            pricing_cache[pname] = fetch_model_pricing(base_url, api_key)
        
        models = pconfig.get("models", {})
        for mname, mconfig in models.items():
            # ... existing model_name extraction logic ...
            
            # --- NEW: Free-only filtering ---
            if pname not in FREE_CREDIT_PROVIDERS:
                # Check if model is free by name pattern
                if not is_model_free(pname, model_name):
                    # Check pricing data
                    p = pricing_cache.get(pname, {})
                    model_pricing = p.get(model_name, {})
                    if model_pricing:
                        # Has pricing data — check if it's $0
                        inp = model_pricing.get("input", 0)
                        out = model_pricing.get("output", 0)
                        if inp > 0 or out > 0:
                            continue  # Skip paid model
                    else:
                        # No pricing data from API — check name patterns
                        # If not matching a free pattern, skip (conservative)
                        if not any(pat in model_name.lower() for pat in FREE_MODEL_PATTERNS):
                            continue
            # --- END NEW ---
            
            # ... rest of existing logic (expensive check, dedup, Target creation) ...
```

**IMPORTANT**: The current code already has a filter for kilo/kilocloud models — it checks
for "free" in the model name. The new logic above replaces that ad-hoc filter with the
general `is_model_free()` function. Make sure to REMOVE the old kilo-specific filter
(if it exists) to avoid duplication.

Look in `load_opencode_targets()` for any existing code like:
```python
if pname in ("kilo", "kilocloud") and "free" not in mname.lower():
    continue
```
Replace it with the new `is_model_free()` check above.

## Step 6: Update `cli.py` `cmd_test()` — add `--include-paid` flag

Add a CLI flag `--include-paid` (default: False) that overrides the free-only filter.
This lets the user intentionally test paid models when they want to.

In the argparse section for the `test` command, add:
```python
p_test.add_argument("--include-paid", action="store_true", 
                     help="Test paid models (not just free ones)")
```

Pass this to `load_all_targets()`:
```python
targets = load_all_targets(
    include_credits=args.include_credits,
    include_expensive=args.include_expensive,
    include_paid=args.include_paid,  # NEW
    provider_filter=args.provider,
    model_filter=args.model,
)
```

Update `load_all_targets()` and `load_opencode_targets()` signatures to accept
`include_paid: bool = False` and skip the pricing/name filter when `include_paid=True`.

## Step 7: Test the changes

1. Run `python3 -c "from config import load_all_targets; ts = load_all_targets(); print(len(ts))"`
   — should show far fewer targets than 508 (probably ~80-120, depending on how many
   hapuppy/blazeai models are free-credit-eligible)
   
2. Run `python3 cli.py list` — verify the model list looks correct:
   - hapuppy: should show ALL models (free credits)
   - blazeai: should show ALL models (free credits)
   - kilocloud: should ONLY show models with "free" in the name
   - opencodezen: should ONLY show models with "free" in name + big-pickle
   - other providers: should only show models with $0 pricing or "free" in name

3. Run `python3 cli.py list --include-paid` — should show ALL 508 targets

## Step 8: Commit and push

```bash
cd /home/owen/llm-speedrun
git add -A
git commit -m "Add free-only model filtering with auto pricing detection"
git push
```

---

## Files to modify
1. **`/home/owen/llm-speedrun/config.py`** — main changes (Steps 1-5)
2. **`/home/owen/llm-speedrun/cli.py`** — add `--include-paid` flag (Step 6)

## Files NOT to modify
- `runner.py` — no changes needed
- `db.py` — no changes needed
- `opencode.jsonc` / `opencode.json` — no changes needed

## Important notes
- The `fetch_model_pricing()` call happens at startup when loading targets. It makes one
  HTTP request per provider. This adds a few seconds to startup but ensures accurate pricing.
- If a provider's `/v1/models` endpoint doesn't return pricing data (most OpenAI-compatible
  endpoints don't), the fallback is to use `FREE_MODEL_PATTERNS` name matching. Models
  without pricing data AND without "free" in the name will be **skipped** (conservative).
- The pricing cache prevents redundant API calls within a single run.
- `FREE_CREDIT_PROVIDERS` is the escape hatch — add provider names there if the user
  confirms they have free credits/quota.
