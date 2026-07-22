#!/usr/bin/env python3
"""Merge AAAK-confident + known-classification entries into provider_registry.json v1.1.

Classifications sourced from:
- AAAK memory blocks (atessa-catalog, working-premium-model-providers, gratisfy-models, etc.)
- Web knowledge (official provider docs for well-known APIs)
- User's prior notes (chinese-llm-welfare-providers-2026-07-02.md, etc.)

CATEGORIES (v1.1):
  unlimited_free         - no quota, no rate limit (rare)
  recurring_auto_refresh - quota resets daily/monthly, no user action needed
  recurring_checkin_required - quota resets w/ manual check-in (Discord, web, etc.)
  finite                 - one-time signup credits, drains to zero, no refresh
  dead                   - host down OR key dead (skip entirely)
  unknown                - default, treat as finite for test budgets
"""
import json
import os
from pathlib import Path

REGISTRY_PATH = Path("~/CodingProjects/llm-speedrun/data/provider_registry.json").expanduser()

# Confident classifications (no websearch needed).
# Sources cited in notes for audit trail.
CONFIDENT = {
    # = recurring_auto_refresh (quota resets, no user action) =
    "alibaba-coding-plan": ("recurring_auto_refresh",
        "Aliyun Dashscope Coding Plan, free monthly quota via subscription. AAAK llm-provider-manager vps_config."),
    "alibaba-coding-plan-cn": ("recurring_auto_refresh",
        "Aliyun Dashscope Coding Plan (CN), free monthly quota via subscription. AAAK llm-provider-manager vps_config."),
    "tencent-coding-plan": ("recurring_auto_refresh",
        "Tencent LKEAP Coding Plan, free monthly quota. AAAK llm-provider-manager vps_config."),
    "zhipuai": ("recurring_auto_refresh",
        "Zhipu BigModel.cn API. Free tier w/ limited daily quota for select models. AAAK bigmodel.cn 20M free Z.ai tokens."),
    "zhipuai-coding-plan": ("recurring_auto_refresh",
        "Zhipu BigModel.cn Coding Plan, free monthly quota via subscription."),
    "zai": ("recurring_auto_refresh",
        "Z.AI (Zhipu international) API. Free tier w/ limited daily quota."),
    "zai-coding-plan": ("recurring_auto_refresh",
        "Z.AI Coding Plan, free monthly quota via subscription."),
    "modelscope": ("recurring_auto_refresh",
        "Alibaba ModelScope, free inference for select open models w/ daily quota."),
    "siliconflow": ("recurring_auto_refresh",
        "SiliconFlow API. Free $14 signup + free models w/ daily rate limit."),
    "siliconflow-cn": ("recurring_auto_refresh",
        "SiliconFlow CN (siliconflow.cn). Same provider, CN endpoint."),
    "kimi-for-coding": ("recurring_auto_refresh",
        "Moonshot Kimi K2 for Coding, free coding plan monthly quota."),
    "minimax-coding-plan": ("recurring_auto_refresh",
        "MiniMax Coding Plan ( internacional), free monthly quota via subscription."),
    "minimax-cn-coding-plan": ("recurring_auto_refresh",
        "MiniMax Coding Plan (CN), free monthly quota via subscription."),
    "kuae-cloud-coding-plan": ("recurring_auto_refresh",
        "Kuae Cloud Coding Plan, free monthly quota via subscription."),
    "xiaomi-token-plan-ams": ("recurring_auto_refresh",
        "Xiaomi Mimo Token Plan (Amsterdam region), free monthly quota."),
    "xiaomi-token-plan-cn": ("recurring_auto_refresh",
        "Xiaomi Mimo Token Plan (CN), free monthly quota."),
    "xiaomi-token-plan-sgp": ("recurring_auto_refresh",
        "Xiaomi Mimo Token Plan (Singapore), free monthly quota."),
    "cohere": ("recurring_auto_refresh",
        "Cohere Trial API key. Free tier: 1000 req/mo, 50 req/min. Auto-refreshes monthly. https://cohere.com/key"),
    "github-models": ("recurring_auto_refresh",
        "GitHub Models. Free tier: limited requests/day per model. https://github.com/marketplace/models"),
    "google": ("recurring_auto_refresh",
        "Google AI Studio Gemini API. Free tier: 15 RPM, 1500 req/day, 1M tokens/min. https://aistudio.google.com"),
    "poe": ("recurring_auto_refresh",
        "Poe API. Daily free quota for some bots + paid subscription for compute bots. https://creator.poe.com"),
    "ollama": ("recurring_auto_refresh",
        "Ollama.com public API. Free limited tier for cloud models w/ daily rate limit."),
    "ollama-alt": ("recurring_auto_refresh",
        "Ollama.com (alt endpoint). Same free tier w/ daily rate limit."),
    "ollama-cloud": ("recurring_auto_refresh",
        "Ollama.com cloud. Free limited tier for cloud models."),
    "pooled": ("recurring_auto_refresh",
        "ai.pooled.dev. Pooled free API endpoint, auto-refreshing daily quota (AAAK working-premium-model-providers)."),
    "tokenrouter": ("recurring_auto_refresh",
        "api.tokenrouter.com. Free tier w/ daily quota (AAAK working-premium-model-providers listed tokenrouter returns empty content but 200 OK)."),

    # = Batch 1 (b19): websearch-confirmed =
    "aihubmix": ("recurring_auto_refresh",
        "27+ free models (GPT-5.5, GPT-Image-2, Gemini 3, GLM-5.1, Kimi, MiniMax, Xiaomi MiMo), daily quota resets, no trial expiry. Source: docs.aihubmix.com/en/blogs/free-ai-models."),
    "kilocode": ("recurring_auto_refresh",
        "Kilo Gateway, `:free` models, anonymous access OK, 200 req/hr per IP rate limit. Source: kilo.ai/docs/gateway/authentication + kilo.ai/pricing."),
    "voidai": ("recurring_auto_refresh",
        "125k tokens/day free per openai-compatible API, Free plan gives basic models. Source: docs.voidai.app/guides/credits + Newelle-LLMS GitHub."),

    # = Batch 2 (m0305) =
    "aihub-071129": ("finite",
        "Chinese NewAPI gateway, ¥7.30 per $1 quota, prepaid credit drains. Source: ai.071129.xyz pricing."),
    "aitools": ("recurring_auto_refresh",
        "19 free models, OpenAI-compatible, no login for API key, 429 on rate limit. Source: platform.aitools.cfd."),
    "banana2556": ("recurring_auto_refresh",
        "公益 API (public-service), free test quota, OpenAI-compatible. Source: api.banana2556.com."),

    # = Batch 3 (m0306) =
    "agnes-ai": ("recurring_auto_refresh",
        "Free tier w/ 30 RPM text, OpenAI-compatible, no expiry. Token Plan tier for paid. Source: apihub.agnes-ai.com."),
    "evolvex": ("recurring_auto_refresh",
        "$0/mo, no credit card, 150+ open models, 5 RPM hard cap. Source: api.evolvex.gg/pricing."),

    # = Batch 4 (m0308-m0309) =
    "navy": ("recurring_auto_refresh",
        "NavyAIUnified $0/day tier w/ 150K tokens/day + 20 RPM, OpenAI-compatible. Source: api.navy pricing."),
    "navy-alt": ("recurring_auto_refresh",
        "NavyAIUnified $0/day tier w/ 150K tokens/day + 20 RPM (alt endpoint). Source: api.navy pricing."),
    "bluesminds": ("recurring_auto_refresh",
        "OmniRoute permanent free plan, 500 pi credits + 20 RPM + 300 RPD, 200+ models. Source: OmniRoute wiki."),

    # = Batch 5 (m0309-m0310) =
    "choosec": ("finite",
        "Chinese NewAPI aggregator, ¥1/$1 top-up gateway, 260+ models, prepaid drains to zero. Source: api.choosec.cn."),
    "llmgateway": ("recurring_auto_refresh",
        "Unified API for 40+ providers, Free tier w/ own keys (no cost) + paid managed. Source: llmgateway.io."),
    "crowllm": ("finite",
        "OpenAI-compatible AI API platform, no clear free-tier pricing. Default-to-finite per user's credit-burn concern. Source: crowllm.com."),

    # = finite (signup credits, drains to zero, no refresh) =
    "novita": ("finite",
        "Novita AI. Free $20 signup credits, no recurring refresh. https://novita.ai/pricing"),
    "llama": ("finite",
        "llama.com Meta official API. Paid only, no free tier confirmed (signup may grant trial credits)."),
    "aerolink": ("finite",
        "$350+ signup credit, INR-priced, rolling 5h/weekly windows, drains to zero. Source: capi.aerolink.lat pricing."),
    "cortecs": ("finite",
        "5% pass-through gateway, EUR prepaid, top up balance, drains to zero, no free tier. Source: cortecs.ai/pricing."),

    # = recurring_checkin_required =
    # (already classified in script: freetheai, nianhua, lotte-library, huashang, lpgpt)

    # = dead (host down, key dead) =
    # AAAK dead-shared-key-providers-2026-07-16
    "setbug": ("dead",
        "Host DNS NX (gone). AAAK dead-shared-key-providers-2026-07-16."),
    "bayunzi": ("dead",
        "Connection timeout. AAAK dead-shared-key-providers-2026-07-16."),
    "conduit": ("dead",
        "Port 443 refused. AAAK dead-shared-key-providers-2026-07-16."),
    "888avi": ("dead",
        "Key 401 invalid. AAAK dead-shared-key-providers-2026-07-16."),
    "anmix": ("dead",
        "Returns HTML not JSON. AAAK dead-shared-key-providers-2026-07-16."),
    "mydamoxing": ("dead",
        "Key 401 invalid. AAAK dead-shared-key-providers-2026-07-16."),
    "tokenaizf": ("dead",
        "Key disabled (401). AAAK dead-shared-key-providers-2026-07-16."),
    "zhangyuapi": ("dead",
        "Key 401 invalid. AAAK dead-shared-key-providers-2026-07-16."),
    "dcapi-gbox": ("dead",
        "Returns HTML not JSON on /v1. AAAK dead-shared-key-providers-2026-07-16."),
    "gbox": ("dead",
        "Returns HTML not JSON on /v1 (duplicate of dcapi-gbox). AAAK dead-shared-key-providers-2026-07-16."),
    "hapuppy": ("dead",
        "Account banned (403 用户已被封禁). AAAK dead-shared-key-providers-2026-07-16."),

    # = unlimited_free (no quota, no rate limit) =
    # (already classified: blazeai, opencode, ovhcloud, llm7, free-gemini, freemodel, airforce, iamhc, opencode_zen, opencode_zen-nokey)

    # = Batch 6 (m0319-m0320) =
    "iflow": ("finite",
        "iFlow search/web API, API key expires in 7 days, search-tool not LLM. Source: api.iflow.cn docs."),
    "iflowcn": ("recurring_checkin_required",
        "iFlow 14 LLM models via OpenAI-compatible. API key expires in 7 days, periodic manual renewal needed. Source: apis.iflow.cn."),
    "jiekou": ("finite",
        "Chinese NewAPI aggregator, signup vouchers + top-up balance + pay-as-you-go. Source: api.jiekou.ai/openai."),
    "nano-gpt": ("finite",
        "x402 accountless crypto payments, no recurring free quota, prepaid balance. Source: nano-gpt.com."),

    # = Batch 7 (m0321) =
    "gcli": ("unlimited_free",
        "GG公益站-云GCLI, Chinese charity/public welfare station, converts GeminiCLI+Antigravity OAuth to OpenAI-compatible. 公益 = charity/freebie. Source: gcli.ggchan.dev."),
    "meganova": ("recurring_auto_refresh",
        "Free registration, no card, 550 msgs/day auto-resets daily 19:00 EST (Manta Mini/Flash/Pro, GLM-4.7-Flash, L3, DeepSeek-V3-Economic, Mistral-Small). Source: api.meganova.ai."),

    # = Inferences from internal evidence (no websearch needed) =
    "blaze-free": ("unlimited_free",
        "api.blazeapi.org/free/v1 endpoint. blazeai already classified unlimited_free; /free/ endpoint is the free variant."),
    "blaze": ("finite",
        "payg.blazeapi.org/v1 = pay-as-you-go endpoint, prepaid credit drains to zero."),
    "blaze-org": ("finite",
        "blazeapi.org/v1 root endpoint (ambiguous). Default-to-finite per user's credit-burn concern."),
    "futureppo": ("recurring_auto_refresh",
        "17nas is the same backend per AAAK; AAAK working-premium-model-providers lists 17nas w/ GPT-5.5 working free."),
    "ktai-paid": ("finite",
        "Paid variant of ktai. Prepaid credit drains."),
    "nova": ("recurring_auto_refresh",
        "api.nova.amazon.com = Amazon Bedrock Nova. AWS free tier 1M tok/mo."),
    "tencent-tokenhub": ("recurring_auto_refresh",
        "Tencent TokenHub free token quota, auto-refreshes. Source: tokenhub.tencentmaas.com."),

    # = Batch 8 (m0328-m0329) =
    "bosco": ("recurring_auto_refresh",
        "Same qzz.io platform operator as qzz.io main entry; qzz classified recurring_auto_refresh w/ 1 req/5min hard cap. Inherits. Source: api.bosco.qzz.io."),
    "ktai": ("finite",
        "Koyeb-hosted NewAPI aggregator. Free Instance 512MB RAM scale-to-zero after 1h idle. LLM calls drain topped-up balance, not recurring free. Source: ktai.koyeb.app."),
    "yanproxy": ("unknown",
        "yanProxy GitHub = light HTTP proxy library NOT LLM. Someone's deployment at .link has no public docs. Default unknown."),
    "yzgpt-alt": ("finite",
        "Zeabur AI Hub pay-as-you-go w/ API key. Someone's NewAPI-on-Zeabur deployment. Prepaid drains. Source: yzgpt.zeabur.app."),
    "yzgpt-main": ("finite",
        "Zeabur AI Hub pay-as-you-go w/ API key (main). Prepaid drains. Source: yzgpt.zeabur.app."),
    "zanity": ("recurring_checkin_required",
        "Made by Voidi (zukijourney's dev), docs.zanity.xyz Premium API 15 RPM. Free Discord role @Zanity Premium # w/ /getkey slash command. Free-via-Discord-role setup. Source: zanity.xyz + docs.zanity.xyz."),
    "zyf": ("finite",
        "zyf.12040414.xyz = N89医费 OpenAI-compatible Chinese relay. ¥5 trial credit for new registrations. LMSpeed free=0. Prepaid drains. Source: zyf.12040414.xyz."),
    "furry": ("finite",
        "ai.furry.edu.gr = PawsAI OpenAI-compatible relay. New registrations include ¥5 trial. Mixed Max/K-proxy pools. Prepaid drains. Source: ai.furry.edu.gr."),
    "redwakeai": ("unknown",
        "redwakeai.vercel.app is subdomain of vercel.app (NOT official Vercel AI Gateway at ai-gateway.vercel.sh). Someone's Vercel deployment, no clear docs. Default unknown."),
    "xinjianya-alt": ("recurring_auto_refresh",
        "punycode xn--kiv260fv3i.cn = Chinese-domain variant of xinjianya. xinjianya free group already classified in registry v1.0. Same backend/operator, alt domain. Inherits."),
    "buddybackend": ("unknown",
        "buddybackend.cloud: no clear docs found anywhere (UCO BuddyGPT unrelated, university HPC). Default unknown."),
}

# Providers to SKIP entirely (search-tool MCP servers, not LLM providers):
SKIP = {
    "brave_search", "duckduckgo", "exa", "google" if False else "google_ok_skip" ,  # keep google above (Gemini AI Studio is an LLM)
    "jina", "tavily",
    "custom",  # = VPS-40 gateway, user's own infra
    "anthropic",  # cc.freemodel.dev sub-variant, freemodel already classified
}

SKIP_FIXED = {
    "brave_search", "duckduckgo", "exa", "jina", "tavily",
    "custom",  # = VPS-40 gateway
    "anthropic",  # cc.freemodel.dev sub-variant
}


def main():
    if not REGISTRY_PATH.exists():
        print(f"ERROR: registry not found at {REGISTRY_PATH}")
        return 1

    with open(REGISTRY_PATH) as f:
        registry = json.load(f)

    providers = registry.get("providers", {})
    updated = 0
    unknown_kept = 0
    skipped = 0

    for name, entry in providers.items():
        if name in SKIP_FIXED:
            skipped += 1
            continue

        if name in CONFIDENT:
            credit_type, note = CONFIDENT[name]
            old = entry.get("credit_type", "unknown")
            entry["credit_type"] = credit_type
            entry["notes"] = note if not entry.get("notes") else f"{entry['notes']} | {note}"
            if old != credit_type:
                updated += 1
        else:
            # Leave as-is (will either be already classified by v1.0/v1.1 script or unknown)
            if entry.get("credit_type") == "unknown":
                unknown_kept += 1

    registry["schema_version"] = "1.1"
    registry["updated"] = "2026-07-22"

    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2, sort_keys=True)

    # Count by credit_type
    counts = {}
    for name, entry in providers.items():
        if name in SKIP_FIXED:
            continue
        ct = entry.get("credit_type", "unknown")
        counts[ct] = counts.get(ct, 0) + 1

    print(f"\n=== Registry v1.1 merge complete ===")
    print(f"Updated:   {updated} providers re-classified")
    print(f"Unknown:   {unknown_kept} providers still unknown (need websearch)")
    print(f"Skipped:   {skipped} non-LLM providers")
    print(f"\nCounts by credit_type:")
    for ct in sorted(counts.keys()):
        print(f"  {ct:30} {counts[ct]:3}")

    # Print unknown list w/ base_url for websearch planning
    print(f"\n=== UNKNOWN providers (need websearch) ===")
    for name, entry in sorted(providers.items()):
        if name in SKIP_FIXED:
            continue
        if entry.get("credit_type") == "unknown":
            url = entry.get("base_url", "?")
            print(f"  {name:35} {url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
