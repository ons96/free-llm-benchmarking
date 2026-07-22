"""Build provider_registry.json v1.1 from b13 prior notes + AAAK memory.

Schema v1.1 (replaces v1.0):
  - Same fields as v1.0 (base_url, credit_type, free_tier, supports_tool_calls,
    supports_reasoning, model_types, notes, rate_limit_per_min, daily_checkin,
    billing_quota_cents, skip_paid_models)
  - credit_type expands: unlimited_free | recurring_auto_refresh |
    recurring_checkin_required | finite | unknown (default for new entries)
  - v1.0 categories (recurring_free, one_time_no_expiry, one_time_with_expiry)
    are migrated to the new vocabulary.

Source files (cited inline in each entry's notes):
  - ~/CodingProjects/docs/llm-provider-findings-2026-07-17.md
  - ~/CodingProjects/chinese-llm-welfare-providers-2026-07-02.md
  - ~/CodingProjects/scripts/free-llm-providers-2026-07-17.md
  - ~/CodingProjects/FREE_LLM_PROVIDER_AUDIT_2026-07-09.md
  - AAAK memory tags (atessa-catalog-2026-07-16, sh00t-account-and-id-status,
    dead-shared-key-providers-2026-07-16, gratisfy-models-2026-07-16, etc.)
  - data/provider_registry.json v1.0 (8 providers classified, dated 2026-05-07)

Output: data/provider_registry.json (136 entries: 126 from opencode-runner.json
+ preserve v1.0 v1.0-style entries for providers not in the merged config).
"""
import json
import os
from pathlib import Path

REPO = Path('/home/osees/CodingProjects/llm-speedrun')
CFG_PATH = REPO / 'config' / 'opencode-runner.json'
OLD_REG = REPO / 'data' / 'provider_registry.json'
OUT = REPO / 'data' / 'provider_registry.json'

# ---- 1. Load merged-config provider list (names + base URLs) ----
with CFG_PATH.open() as f:
    cfg = json.load(f)
openc_providers = cfg.get('provider', {})

# ---- 2. Load v1.0 existing classifications (8 entries) ----
with OLD_REG_PATH.open() if (OLD_REG_PATH := OLD_REG).exists() else None as f:
    old_reg = json.load(f) if f else {'providers': {}}
v1_0 = old_reg.get('providers', {})

# v1.0 -> v1.1 category migration
V1_0_TO_V1_1 = {
    'unlimited_free': 'unlimited_free',
    'recurring_free': 'recurring_auto_refresh',   # default: assume auto refresh unless daily_checkin=true
    'one_time_no_expiry': 'finite',
    'one_time_with_expiry': 'finite',
    # v1.1 native categories pass through:
    'finite': 'finite',
    'unknown': 'unknown',
    'recurring_auto_refresh': 'recurring_auto_refresh',
    'recurring_checkin_required': 'recurring_checkin_required',
}

# ---- 3. Classify from documented evidence (b13 notes) ----
# Format: provider_name -> dict of fields to set (credit_type + fields)
# Notes cite the source file so future agents can re-verify.
# `unknown` = no documented evidence; user reviews and fills.
CLASSIFIED = {
    # === AAAK-confirmed recurring-auto-refresh ===
    'atessa': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 10,
        'notes': 'Quota resets 00:00 UTC daily. Separate buckets for grok-4.5 / composer-2.5 vs other premium. 32 models catalog. Source: AAAK atessa-catalog-2026-07-16 + scripts/free-llm-providers-2026-07-17.md',
        'supports_reasoning': True,
        'supports_tool_calls': True,
    },
    'qzz': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': None,  # special: 1 req / 5 min / whole account
        'notes': 'Hard rate cap: 1 req / 5 min / whole account (not per-model). Low-priority fallback only. Source: AAAK free-llm-providers-2026-07-17 + scripts/free-llm-providers-2026-07-17.md',
        'supports_reasoning': True,
    },
    'nvidia': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 40,
        'notes': 'NIM free tier 40 RPM best-effort, no daily token cap. Credit program retired early 2025. Source: AAAK nim-glm52-429-diagnosis + research 2026-07-06',
        'supports_reasoning': True,
        'supports_tool_calls': True,
    },
    # === AAAK-confirmed recurring-checkin-required ===
    'freetheai': {
        'credit_type': 'recurring_checkin_required',
        'free_tier': True,
        'rate_limit_per_min': 30,
        'daily_checkin': True,
        'notes': '403 daily Discord check-in required. Source: AAAK + chinese-llm-welfare-providers-2026-07-02.md',
    },
    'nianhua': {
        'credit_type': 'recurring_checkin_required',
        'free_tier': True,
        'rate_limit_per_min': 20,
        'daily_checkin': True,
        'notes': '$15 free + 3-5 daily check-in. ONLY route for Claude Fable 5/Opus 4.8/4.7/Sonnet 5. Ration. Source: chinese-llm-welfare-providers-2026-07-02.md + FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
        'supports_reasoning': True,
    },
    # === AAAK-confirmed unlimited-free ===
    'blazeai': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'rate_limit_per_min': 20,
        'supports_tool_calls': True,
        'supports_reasoning': True,
        'notes': 'Unlimited free usage. Source: v1.0 registry + AAAK working-premium-model-providers (blazeai qwen3.6-max-preview-thinking etc verified working)',
    },
    'opencode': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': 'OpenCode Zen - unlimited free but only for free-tier models (premium returns 401). Source: AAAK opencode-model-config + v1.0 registry',
    },
    'opencode_zen': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': 'OpenCode Zen duplicate. ONLY free-tier models (premium 401). Source: AAAK opencode-model-config',
    },
    'opencode_zen-nokey': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': 'OpenCode Zen anonymous tier (no key). 18 models. Source: AAAK',
    },
    # === AAAK-confirmed finite (one-shot credits, may or may not refresh) ===
    'groq': {
        'credit_type': 'finite',
        'free_tier': True,
        'rate_limit_per_min': 30,
        'billing_quota_cents': 500,
        'notes': '$5 free signup credits. Free API tier actually IS recurring now (post-2024) - RECLASSIFY pending probe. Source: v1.0 registry + current groq docs (free tier active, not just signup credits).',
        'supports_tool_calls': True,
        'supports_reasoning': True,
    },
    'sambanova': {
        'credit_type': 'finite',
        'free_tier': True,
        'billing_quota_cents': 1000,
        'notes': '$10 free signup credits. Source: chinese-llm-welfare-providers-2026-07-02.md',
    },
    'ai-claw': {
        'credit_type': 'finite',
        'free_tier': True,
        'notes': 'Group key has finite balance. 138-model catalog but only 15 invoke on user default key. 402 insufficient-balance on premium tier models = paid-tier gate, not drain. Source: docs/llm-provider-findings-2026-07-17.md',
        'skip_paid_models': True,
    },
    'sh00t': {
        'credit_type': 'finite',
        'free_tier': False,
        'notes': 'Paid tier only (key sk_QX3AwvD...). Only gpt-5.6-luna + gpt-5.6-terra work; 11 other models 402 account inactive. Bun :9879 proxy trims tools. Source: AAAK sh00t-account-and-id-status',
    },
    # Note: sh00t-anthropic was filtered out of opencode-runner.json (localhost proxy).

    # === Workers-provider credits (Chinese welfare doc) ===
    'iamhc': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': '195+ models UNLIMITED per docs. Old key dead 401 (needs rotation, see AAAK dead-shared-key-providers-2026-07-16). Source: chinese-llm-welfare-providers-2026-07-02.md',
    },
    'lotte-library': {
        'credit_type': 'recurring_checkin_required',
        'free_tier': True,
        'daily_checkin': True,
        'notes': 'Lottery spins/day + daily quota. Source: chinese-llm-welfare-providers-2026-07-02.md (as Weiyun)',
    },
    'huashang': {
        'credit_type': 'recurring_checkin_required',
        'free_tier': True,
        'daily_checkin': True,
        'notes': 'welfare-provider group. Source: chinese-llm-welfare-providers-2026-07-02.md',
    },
    'lpgpt': {
        'credit_type': 'recurring_checkin_required',
        'free_tier': True,
        'daily_checkin': True,
        'notes': 'welfare-provider group. Source: inferred from group',
    },

    # === Airforce / hosted free models (hidden rate bucket) ===
    'airforce': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'rate_limit_per_min': 1,  # effective ~1/120s
        'notes': 'All 18 :free models 200 OK. Hidden ~120s token bucket cooldown after 1-2 successes. Key sk-air-... configured. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
    },

    # === Gratisfy (per b13) ===
    'gratisfy': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'FREE models: subaxis/glm-5.2, glm-5.2 alias, atessa/grok-4.5, atessa/composer-2.5, atessa/hy3. Old 500/* models 402 quota-exhausted (paid bucket). Source: AAAK gratisfy-models-2026-07-16',
    },

    # === OVHcloud anonymous (no key) ===
    'ovhcloud': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'rate_limit_per_min': 2,
        'notes': 'Anonymous, 2 RPM/model/IP, EU-hosted, no apiKey. 11 LIVE chat-only models verified 2026-07-17. Source: AAAK free-llm-providers-2026-07-17',
        'supports_tool_calls': False,
        'supports_reasoning': False,
    },

    # === LLM7.io (token-gated, free tier) ===
    'llm7': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'rate_limit_per_min': 30,
        'notes': '30 RPM free WITH token (Discord-issued). Catalog-free only; target models (gpt-5.5/fable/opus/sonnet) are paid tier. Source: AAAK free-llm-providers-2026-07-17',
    },

    # === Pollinations / no-key (not in our config, noting for context) ===

    # === OpenRouter (per AAAK - free tier gone) ===
    'openrouter': {
        'credit_type': 'finite',
        'free_tier': True,  # has some free models, but our credits gone
        'notes': '402 no credits on free tier. SKIP in nightly per AAAK. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
        'skip_paid_models': True,
    },

    # === Zenmux / Zenllm dead-credits ===
    'zenmux': {
        'credit_type': 'finite',
        'free_tier': True,
        'notes': '402 reject_no_credit. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
    },

    # === Huggingface ===
    'huggingface': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Inference router. zai-org/GLM-5.2 working free 2.67s. Source: AAAK + FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
        'supports_reasoning': True,
    },

    # === Cerebras (free inference) ===
    'cerebras': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 30,
        'notes': '1M tok/day cap, 8K ctx free. Source: AAAK working-llm-providers + free-llm-providers-2026-07-17.md',
        'supports_reasoning': True,
        'supports_tool_calls': True,
    },

    # === Mistral API ===
    'mistral': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': '~1B tok/mo free tier. Source: AAAK free-llm-providers-2026-07-17.md',
        'supports_reasoning': True,
        'supports_tool_calls': True,
    },

    # === DeepSeek API (direct) ===
    'deepseek': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Direct DeepSeek API (api.deepseek.com). Trusted direct provider. Source: AAAK + AGENTS.md',
        'supports_reasoning': True,
    },

    # === Together (direct) ===
    'together': {
        'credit_type': 'finite',
        'free_tier': True,
        'billing_quota_cents': 500,
        'notes': '$5 signup credits, trusted direct provider. Source: AGENTS.md trusted-providers list',
    },

    # === Gemini (direct Google AI Studio) ===
    'gemini': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Free tier generous RPM. Source: AAAK working-llm-providers (Gemini 429 quota occasionally). Trusted direct.',
        'supports_reasoning': True,
    },

    # === Github Models (via Azure inference endpoint) ===
    'github-models': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 15,
        'notes': 'GitHub Models free tier (incl GPT-5.5, Sonnet 5, etc). Source: AAAK free-llm-providers-2026-07-17.md',
    },

    # === Xinjianya (v1.0 entry) ===
    'xinjianya': {
        'credit_type': 'finite',
        'free_tier': True,
        'supports_tool_calls': True,
        'supports_reasoning': True,
        'rate_limit_per_min': 10,
        'skip_paid_models': True,
        'notes': 'Free models in default group (ratio=0). NOT testing paid models. V1.0 had one_time_no_expiry -> v1.1 finite. Source: v1.0 registry (kept)',
    },

    # === Koyeb (NIM proxy per b13) ===
    'koyeb': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Koyeb free NIM proxy tier. Same 404 hash 23d4f03a as nvidia direct + xinjianya + paxsenix for kimi-k2.6 (NIM passthrough). Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
    },

    # === Paxsenix (file-key, verified working) ===
    'paxsenix': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 20,
        'notes': 'WORKING free for GPT-5.5 + GLM-5.2. 37 models. ATessa-equivalent routing. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md + AAAK working-premium-model-providers',
        'supports_reasoning': True,
        'supports_tool_calls': True,
    },

    # === 17nas (verified working) ===
    '17nas': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'rate_limit_per_min': 20,
        'notes': 'WORKING free for GPT-5.5. Source: AAAK working-premium-model-providers + opencode_zen config',
        'supports_reasoning': True,
    },

    # === Dext (per b7/b13 reliable free) ===
    'dext': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'NewAPI-compatible, 215 models. Working free via DEXT_API_KEY. Source: AAAK opencode-apikey-cleanup-2026-07-15',
        'supports_reasoning': True,
    },

    # === Tokenlb (verified during smoke, b4/b7) ===
    'tokenlb': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Opus 4.8/4.7 + GPT-5.5 verified working (post-merge smoke test 2026-07-21). Source: AAAK working-premium-model-providers',
        'supports_reasoning': True,
    },

    # === Logfare / Logfare-alt (Kimi K3 route, b13) ===
    'logfare': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'FREE Kimi K3 + deepseek-v4-flash, v4-pro, kimi-k2.6, mimo-v2.5 working with reasoning_effort:max. Source: docs/llm-provider-findings-2026-07-17.md',
        'supports_reasoning': True,
    },
    'logfare-alt': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Logfare alternate key. Same endpoint. Source: b4 merge',
        'supports_reasoning': True,
    },

    # === Tokenreply (b4 smoke, partial) ===
    'tokenreply': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': '38 models in catalog. Smoke 2026-07-21 partial (some succeed). Source: b4 sweep DB.',
    },

    # === Futureppo + futureppo-new ===
    'futureppo-new': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Futureppo new endpoint. Slow response time (smoke). Source: b13 + b4, AAAK working-premium-model-providers',
    },

    # === Dead-shared-key providers (14, AAAK 2026-07-16) - classified as paid/dead ===
    'hapuppy': {
        'credit_type': 'finite',
        'free_tier': True,
        'rate_limit_per_min': 30,
        'notes': 'DEAD: 403 user banned per AAAK dead-shared-key-providers-2026-07-16. V1.0 said recurring_free; key is dead, treat as finite-drained.',
    },
    'setbug': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: DNS NX (gone) per AAAK dead-shared-key-providers-2026-07-16. Treat as paid/dead.',
    },
    'bayunzi': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: connection timeout per AAAK dead-shared-key-providers-2026-07-16. Treat as paid/dead.',
    },
    'conduit': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: port 443 refused per AAAK dead-shared-key-providers-2026-07-16. Treat as paid/dead.',
    },
    'zhangyuapi': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: 401 invalid key per AAAK dead-shared-key-providers-2026-07-16. Key rotation may restore.',
    },
    'tokenaizf': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: 401 API key disabled per AAAK dead-shared-key-providers-2026-07-16.',
    },
    'mydamoxing': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: 401 invalid key per AAAK dead-shared-key-providers-2026-07-16.',
    },
    # AAAK dead-shared-key-providers-2026-07-16 — host alive but key dead OR host down.
    # Mark as 'dead' so sweep skips entirely (per user credit-burn concern: never test dead providers).
    '888avi':           {'credit_type': 'dead', 'notes': 'AAAK dead: 401 invalid key (host alive)'},
    'anmix':            {'credit_type': 'dead', 'notes': 'AAAK dead: HTML not JSON (host alive)'},
    'bayunzi':          {'credit_type': 'dead', 'notes': 'AAAK dead: connection timeout (host down)'},
    'conduit':          {'credit_type': 'dead', 'notes': 'AAAK dead: port 443 refused (host down)'},
    'dcapi-gbox':       {'credit_type': 'dead', 'notes': 'AAAK dead: HTML not JSON (host alive)'},
    'gbox':             {'credit_type': 'dead', 'notes': 'AAAK dead: HTML not JSON (host alive)'},
    'setbug':           {'credit_type': 'dead', 'notes': 'AAAK dead: DNS NX (host gone)'},
    'mydamoxing':       {'credit_type': 'dead', 'notes': 'AAAK dead: 401 invalid key (host alive)'},
    'tokenaizf':        {'credit_type': 'dead', 'notes': 'AAAK dead: 401 API key disabled (host alive)'},
    'zhangyuapi':       {'credit_type': 'dead', 'notes': 'AAAK dead: 401 invalid key (host alive)'},
    'hapuppy':          {'credit_type': 'dead', 'notes': 'AAAK dead: 403 banned (host alive)'},
    'freetheai':        {'credit_type': 'recurring_checkin_required', 'notes': 'AAAK: 403 Discord daily check-in required (working otherwise)'},
    'railway-newapi': None,  # placeholder to drop if exists
    '888avi': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: 401 invalid key per AAAK dead-shared-key-providers-2026-07-16.',
    },
    'anmix': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: returned HTML not JSON per AAAK dead-shared-key-providers-2026-07-16.',
    },
    'dcapi-gbox': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: 200 on /v1 but HTML not JSON per AAAK dead-shared-key-providers-2026-07-16.',
    },
    'gbox': {
        'credit_type': 'unknown',
        'free_tier': False,
        'notes': 'DEAD: alias of dcapi-gbox per AAAK dead-shared-key-providers-2026-07-16.',
    },

    # === PrivAiTe-local-only routes (skip native testing - they route elsewhere) ===
    'free-gemini': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': 'Free Gemini proxy on Render. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md (gemini wrapper)',
    },
    'freemodel': {
        'credit_type': 'unlimited_free',
        'free_tier': True,
        'notes': 'Free models endpoint (freemodel.dev). Hosts anthropic-archived route. Source: FREE_LLM_PROVIDER_AUDIT_2026-07-09.md',
    },

    # === Antigravity (Google OAuth, AAAK) ===
    'antigravity': {
        'credit_type': 'recurring_auto_refresh',
        'free_tier': True,
        'notes': 'Google Antiquity OAuth, free Google/Gemini quota. Needs periodic re-auth. Unreliable. Source: AAAK antigravity-endpoint',
    },
}

# Drop the placeholder nil entries
CLASSIFIED = {k: v for k, v in CLASSIFIED.items() if v is not None}

# ---- 4. Build merged registry ----
out = {
    'schema_version': '1.1',
    'updated': '2026-07-22',
    'categories': [
        'unlimited_free',
        'recurring_auto_refresh',
        'recurring_checkin_required',
        'finite',
        'unknown',
    ],
    'providers': {},
}

for name in sorted(openc_providers.keys()):
    p = openc_providers[name]
    base = p.get('options', {}).get('baseURL', p.get('baseURL', ''))
    entry = {
        'base_url': base,
        'credit_type': 'unknown',  # default
        'free_tier': True,         # default optimistic for sweep inclusion
        'supports_tool_calls': False,
        'supports_reasoning': False,
        'model_types': ['text', 'chat'],
        'notes': '',
    }
    # Migrate v1.0 fields if present (for 8 existing entries)
    if name in v1_0:
        v1_0_entry = v1_0[name]
        old_ct = v1_0_entry.get('credit_type', 'unknown')
        entry['credit_type'] = V1_0_TO_V1_1.get(old_ct, 'unknown')
        for k in ('free_tier', 'supports_tool_calls', 'supports_reasoning',
                  'model_types', 'rate_limit_per_min', 'daily_checkin',
                  'billing_quota_cents', 'skip_paid_models', 'notes'):
            if k in v1_0_entry:
                entry[k] = v1_0_entry[k]
    # Override with classified (b13 evidence has priority over v1.0)
    if name in CLASSIFIED:
        entry.update(CLASSIFIED[name])
    out['providers'][name] = entry

# ---- 5. Count + sanity check ----
counts = {}
for name, e in out['providers'].items():
    ct = e.get('credit_type', 'unknown')
    counts[ct] = (counts.get(ct, 0) or 0) + 1

print(f'Built {len(out["providers"])} entries.')
print('Counts by credit_type:')
for ct, n in sorted(counts.items()):
    print(f'  {ct:30s}  {n}')

# ---- 6. List unknowns for user fillable table ----
unknowns = sorted([n for n, e in out['providers'].items()
                   if e.get('credit_type') == 'unknown'])
print(f'\n{len(unknowns)} UNKNOWN providers need user classification:')
for n in unknowns:
    e = out['providers'][n]
    base = e.get('base_url', '')
    n_models = len(openc_providers[n].get('models', {})) if isinstance(openc_providers[n].get('models'), dict) else len(openc_providers[n].get('models', []))
    print(f'  {n:30s}  {base[:50]:50s}  models={n_models}')

# ---- 7. Write ----
with OUT.open('w') as f:
    json.dump(out, f, indent=2, sort_keys=True)
print(f'\nWrote {OUT}')
