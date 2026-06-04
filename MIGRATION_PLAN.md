# Free-LLM-Benchmarking: API Key Migration & Full Benchmark Plan

## Overview
Migrate from single `OPENCODE_JSON_BASE64` secret (containing hardcoded keys inside gzipped JSON) to **individual GitHub Actions secrets per provider** (Option 1). This is more secure (each key isolated), safer (no single blast radius), and fully scriptable via `gh secret set`.

---

## Current State

### Secrets
- `OPENCODE_JSON_BASE64` — gzipped+base64 opencode.json with hardcoded API keys (current, to be removed)

### Local Files
- `~/.config/opencode/opencode.json` — 93 providers, all using `$ENV_VAR` references (no real keys)
- `~/.config/opencode/.env` — 66 env var entries; 26 have real keys, 40 have `${VAR}` references that need resolving

### Provider Status (66 env vars needed)
- **24 already resolved** (have real key values from VPS/local)
- **42 unresolved** (`${VAR}` references needing actual values from VPS gateway or other sources)

### Key Resolution Sources
- `VPS:~/LLM-API-Key-Proxy/.env` — 47 entries with real keys
- `VPS:~/.config/opencode/secrets.env` — 2 entries (SUPACODER, NVIDIA)
- `~/CodingProjects/ideas-dispatcher/.env` — local env file
- `~/.config/opencode/opencode.json.backup_20260419_170544` — backup with 10 real keys

---

## Phase 1: Resolve All API Keys

### 1A. Resolve the 26 already-available keys
These are already in `~/.config/opencode/.env` with real values. Just need to be collected into the final secrets script.

**Already resolved (26 keys):**
```
BLAZEAI_API_KEY, CEREBRAS_API_KEY, FREETHEAI_API_KEY, GEMINI_API_KEY,
GROQ_API_KEY, HUASHANG_API_KEY, LOGFARE_API_KEY, LOTTE_LIBRARY_API_KEY,
MISTRAL_API_KEY, NVIDIA_API_KEY, OPENCODE_API_KEY, OPENCODE_ZEN_API_KEY,
OPENROUTER_API_KEY, SUPACODER_API_KEY, TOGETHER_API_KEY, XINJIANYA_API_KEY,
ZANITY_API_KEY
```
Plus VPS-resolved keys that can fill some `${VAR}` references:
```
HAPUPPY_API_KEY (from VPS), KILO_API_KEY (from VPS), KILOCLOUD_API_KEY (from VPS),
KTAI_API_KEY (from VPS), KTAI_PAID_API_KEY (from VPS), OLLAMA_CLOUD_API_KEY (from VPS),
SWIFTROUTER_API_KEY (from VPS), WIWI_API_KEY (from VPS), BLUESMINDS_API_KEY (from VPS),
CARTER_API_KEY=<REDACTED-from-VPS-backup-rotate-if-still-active>
```

### 1B. Resolve the remaining keys from VPS gateway
SSH to VPS and extract keys from the gateway's provider config:

```bash
# Fetch ALL env vars from VPS
ssh -i ~/.ssh/oracle.key ubuntu@155.248.217.255 "cat ~/LLM-API-Key-Proxy/.env"
# Also check gateway source code for embedded keys
ssh -i ~/.ssh/oracle.key ubuntu@155.248.217.255 "ls ~/LLM-API-Key-Proxy/"
```

### 1C. Resolve compound references
Some env vars serve multiple providers. The `${VAR}` references need to be dereferenced:
- `${KILOCloud_API_KEY}` → KILOCLOUD_API_KEY (same key for: kilo, kilocloud, aitools)
- `${ALIBABA_API_KEY}` → need to find ALIBABA_API_KEY (serves: alibaba-cn, alibaba-coding-plan, alibaba-coding-plan-cn)
- `${SILICONFLOW_API_KEY}` → need to find (serves: siliconflow, siliconflow-cn)
- `${TENCENT_API_KEY}` → need to find (serves: tencent-coding-plan, tencent-tokenhub)
- `${XIAOMI_API_KEY}` → need to find (serves: xiaomi-token-plan-ams/cn/sgp)
- `${ZAI_API_KEY}` → need to find (serves: zai, zai-coding-plan)
- `${ZHIPUAI_API_KEY}` → need to find (serves: zhipuai, zhipuai-coding-plan)
- `${MINIMAX_API_KEY}` → need to find (serves: minimax-coding-plan, minimax-cn-coding-plan)
- `${KIMI_API_KEY}` → need to find (serves: kimi-for-coding)
- `${GITHUB_TOKEN}` → use GitHub token (serves: github-models)
- `${HF_API_KEY}` → need to find (serves: huggingface)
- `${AMAZON_NOVA_API_KEY}` → need to find (serves: nova)
- `${NANOGPT_API_KEY}` → need to find (serves: nano-gpt)
- `${KUAE_API_KEY}` → need to find (serves: kuae-cloud-coding-plan)

### 1D. Still-unresolved providers (keys not found anywhere yet)
These 30 base env vars have NO known source:
```
ALIBABA_API_KEY, CLIPROXYAPI_API_KEY, COHERE_API_KEY, CORTECS_API_KEY,
CURSOR_PROXY_API_KEY, DEEPSEEK_API_KEY, GITHUB_TOKEN, HF_API_KEY,
IFLOW_API_KEY, JIEKOU_API_KEY, KIMI_API_KEY, KUAE_API_KEY, LLAMA_API_KEY,
LLM7_API_KEY, LLMGATEWAY_API_KEY, MEGANOVA_API_KEY, MINIMAX_API_KEY,
MODELSCOPE_API_KEY, MYDAMOXING_API_KEY, NANOGPT_API_KEY, AMAZON_NOVA_API_KEY,
POE_API_KEY, SAMBANOVA_API_KEY, SILICONFLOW_API_KEY, TENCENT_API_KEY,
XIAOMI_API_KEY, ZAI_API_KEY, ZENMUX_API_KEY, ZHIPUAI_API_KEY, CLAUDE_CARTER_API_KEY
```

**ACTION REQUIRED:** You need to provide these keys or confirm they should be excluded. Many of these providers were set up through the VPS LLM-API-Key-Proxy gateway which may have them in its internal config/database — check `~/LLM-API-Key-Proxy/` on the VPS for a SQLite DB, config file, or environment file that has these values.

**Quick check command:**
```bash
ssh -i ~/.ssh/oracle.key ubuntu@155.248.217.255 \
  "grep -r 'API_KEY\|api_key\|apiKey' ~/LLM-API-Key-Proxy/ --include='*.py' --include='*.json' --include='*.yaml' --include='*.toml' --include='*.env' -l"
```

### 1E. Providers that can be skipped entirely
These providers are in `SKIP_PROVIDERS` and don't need keys:
```
cliproxyapi (SKIP - dead), iflow (SKIP - shut down), llmgateway (SKIP - 401),
zenmux (SKIP - 401), kilocloud (SKIP - dead/rate limited)
```
Their env vars can be left empty or removed from the secrets list.

---

## Phase 2: Create Final Flat .env File

Build `~/.config/opencode/.env.final` with all resolved keys (no `${VAR}` references):

```bash
# After resolving all keys in Phase 1, produce a flat .env:
# Format: PROVIDERNAME_API_KEY=actual_key_value
# One line per unique key
# No ${VAR} references, no comments with keys
```

**Expected: ~40-50 unique secrets** (after deduplicating shared keys and removing SKIP providers)

---

## Phase 3: Upload Secrets to GitHub Repo (Option 1)

### 3A. Authenticate gh CLI
```bash
echo "<REDACTED-gh-PAT>" | gh auth login --with-token
```

### 3B. Script to upload all secrets
```bash
#!/bin/bash
# upload_secrets.sh — set each API key as an individual GitHub Actions secret
REPO="ons96/free-llm-benchmarking"

while IFS='=' read -r name value; do
    # Skip comments and empty lines
    [[ "$name" =~ ^#.*$ ]] && continue
    [[ -z "$name" || -z "$value" ]] && continue
    
    # Set as GitHub secret
    echo "Setting secret: $name"
    echo "$value" | gh secret set "$name" --repo "$REPO"
    
    # Rate limit: small delay to avoid API throttling
    sleep 0.5
done < ~/.config/opencode/.env.final

echo "Done! Verify with: gh secret list --repo $REPO"
```

### 3C. Remove old secret
```bash
gh secret delete OPENCODE_JSON_BASE64 --repo ons96/free-llm-benchmarking
```

---

## Phase 4: Update Workflow to Use Individual Secrets

### 4A. Modify `.github/workflows/benchmark.yml`

**Replace the "Restore opencode config from secret" step with:**

```yaml
- name: Restore opencode config
  run: |
    mkdir -p ~/.config/opencode
    # opencode.json uses $ENV_VAR references — it's safe to commit
    cp .github/opencode-config.json ~/.config/opencode/opencode.json
    echo "Config restored. Providers: $(python3 -c "import json; print(len(json.load(open('$HOME/.config/opencode/opencode.json')).get('provider',{})))")"

- name: Set API keys from secrets
  run: |
    # Each provider's API key is an individual GitHub Actions secret
    # opencode reads env vars for $VAR references in its config
    {
      echo "BLAZEAI_API_KEY=${{ secrets.BLAZEAI_API_KEY }}"
      echo "CEREBRAS_API_KEY=${{ secrets.CEREBRAS_API_KEY }}"
      echo "FREETHEAI_API_KEY=${{ secrets.FREETHEAI_API_KEY }}"
      echo "GEMINI_API_KEY=${{ secrets.GEMINI_API_KEY }}"
      echo "GROQ_API_KEY=${{ secrets.GROQ_API_KEY }}"
      echo "HAPUPPY_API_KEY=${{ secrets.HAPUPPY_API_KEY }}"
      echo "HUASHANG_API_KEY=${{ secrets.HUASHANG_API_KEY }}"
      echo "LOGFARE_API_KEY=${{ secrets.LOGFARE_API_KEY }}"
      echo "LOTTE_LIBRARY_API_KEY=${{ secrets.LOTTE_LIBRARY_API_KEY }}"
      echo "MISTRAL_API_KEY=${{ secrets.MISTRAL_API_KEY }}"
      echo "NVIDIA_API_KEY=${{ secrets.NVIDIA_API_KEY }}"
      echo "OPENCODE_API_KEY=${{ secrets.OPENCODE_API_KEY }}"
      echo "OPENCODE_ZEN_API_KEY=${{ secrets.OPENCODE_ZEN_API_KEY }}"
      echo "OPENROUTER_API_KEY=${{ secrets.OPENROUTER_API_KEY }}"
      echo "TOGETHER_API_KEY=${{ secrets.TOGETHER_API_KEY }}"
      echo "XINJIANYA_API_KEY=${{ secrets.XINJIANYA_API_KEY }}"
      echo "ZANITY_API_KEY=${{ secrets.ZANITY_API_KEY }}"
      echo "OLLAMA_CLOUD_API_KEY=${{ secrets.OLLAMA_CLOUD_API_KEY }}"
      echo "SWIFTROUTER_API_KEY=${{ secrets.SWIFTROUTER_API_KEY }}"
      echo "WIWI_API_KEY=${{ secrets.WIWI_API_KEY }}"
      echo "BLUESMINDS_API_KEY=${{ secrets.BLUESMINDS_API_KEY }}"
      echo "CLAUDE_CARTER_API_KEY=${{ secrets.CLAUDE_CARTER_API_KEY }}"
      echo "KTAI_API_KEY=${{ secrets.KTAI_API_KEY }}"
      echo "KTAI_PAID_API_KEY=${{ secrets.KTAI_PAID_API_KEY }}"
      echo "KILO_API_KEY=${{ secrets.KILO_API_KEY }}"
      echo "KILOCLOUD_API_KEY=${{ secrets.KILOCLOUD_API_KEY }}"
      echo "AITOOLS_API_KEY=${{ secrets.AITOOLS_API_KEY }}"
      echo "ALIBABA_API_KEY=${{ secrets.ALIBABA_API_KEY }}"
      echo "SILICONFLOW_API_KEY=${{ secrets.SILICONFLOW_API_KEY }}"
      echo "TENCENT_API_KEY=${{ secrets.TENCENT_API_KEY }}"
      echo "XIAOMI_API_KEY=${{ secrets.XIAOMI_API_KEY }}"
      echo "ZAI_API_KEY=${{ secrets.ZAI_API_KEY }}"
      echo "ZHIPUAI_API_KEY=${{ secrets.ZHIPUAI_API_KEY }}"
      echo "MINIMAX_API_KEY=${{ secrets.MINIMAX_API_KEY }}"
      echo "KIMI_API_KEY=${{ secrets.KIMI_API_KEY }}"
      echo "GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}"
      echo "HF_API_KEY=${{ secrets.HF_API_KEY }}"
      echo "COHERE_API_KEY=${{ secrets.COHERE_API_KEY }}"
      echo "DEEPSEEK_API_KEY=${{ secrets.DEEPSEEK_API_KEY }}"
      echo "SAMBANOVA_API_KEY=${{ secrets.SAMBANOVA_API_KEY }}"
      echo "POE_API_KEY=${{ secrets.POE_API_KEY }}"
      echo "LLAMA_API_KEY=${{ secrets.LLAMA_API_KEY }}"
      echo "MODELSCOPE_API_KEY=${{ secrets.MODELSCOPE_API_KEY }}"
      echo "CORTECS_API_KEY=${{ secrets.CORTECS_API_KEY }}"
      echo "CURSOR_PROXY_API_KEY=${{ secrets.CURSOR_PROXY_API_KEY }}"
      # Add more as keys are found
    } >> ~/.config/opencode/.env
    
    echo "API keys configured: $(grep -c '^[A-Z].*=..*$' ~/.config/opencode/.env) keys set"

- name: Run benchmarks
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    # Source the env file so opencode can expand $VAR references
    set -a
    source ~/.config/opencode/.env
    set +a
    
    ARGS="--runs ${{ github.event.inputs.runs || 1 }} --concurrency-auto"
    # ... rest of args unchanged ...
    uv run python cli.py test $ARGS -y
```

**IMPORTANT:** The `env:` block on the "Run benchmarks" step should NOT list individual secrets anymore. Instead, secrets are written to `~/.config/opencode/.env` and sourced. This keeps the workflow YAML clean regardless of how many providers are added.

### 4B. Commit opencode.json to the repo
Since opencode.json now only contains `$ENV_VAR` references (no real keys), it's safe to commit:

```bash
cp ~/.config/opencode/opencode.json /home/osees/CodingProjects/llm-speedrun/.github/opencode-config.json
git add .github/opencode-config.json
git commit -m "chore: add env-var-only opencode config (no secrets)"
```

### 4C. Add repo guard
Add to the workflow job:
```yaml
jobs:
  benchmark:
    name: Run LLM Benchmarks
    runs-on: ubuntu-latest
    if: github.repository == 'ons96/free-llm-benchmarking'  # <-- ADD THIS
    timeout-minutes: 300
```

This prevents forks from accessing the secrets.

---

## Phase 5: Update Runner to Support .env Expansion

The opencode binary expands `$VAR` references in its config using environment variables. The benchmark runner (`runner.py`) calls opencode's API. Verify that the runner's process inherits the env vars from the sourced `.env` file.

**Check:** Does `uv run python cli.py test` properly inherit env vars from the shell? It should, since `source .env && uv run ...` puts vars in the process environment.

If opencode's Go binary needs a `.env` file instead of shell env vars, the workflow already writes one at `~/.config/opencode/.env`. Verify opencode reads from this path.

---

## Phase 6: Trigger Full Benchmark Run

### 6A. Manual trigger
```bash
cd /home/osees/CodingProjects/llm-speedrun
gh workflow run benchmark.yml --repo ons96/free-llm-benchmarking \
  -f runs=1 -f skip-tested=false -f retest-suspicious=true
```

### 6B. Verify coverage
After the run, check:
- How many unique providers were tested (should be 40+ instead of 5)
- How many models appear in the leaderboard (was 73, should increase)
- Any providers that fail auth → add to SKIP_PROVIDERS if permanently broken

---

## Phase 7: Clean Up

1. Delete local `~/.config/opencode/.env` backup with real keys after migration
2. Delete `OPENCODE_JSON_BASE64` secret from GitHub
3. Remove the backup `opencode.json.backup_*` files from local machine
4. Update `.gitignore` to exclude any `.env` files
5. Verify no real API keys exist anywhere in the git history:
   ```bash
   git log --all --diff-filter=D -- '*.env' '*.key' 'credentials*'
   ```

---

## Summary of Secrets to Create

| # | Secret Name | Source | Status |
|---|-------------|--------|--------|
| 1 | BLAZEAI_API_KEY | local .env | READY |
| 2 | CEREBRAS_API_KEY | local .env | READY |
| 3 | FREETHEAI_API_KEY | local .env | READY |
| 4 | GEMINI_API_KEY | local .env | READY |
| 5 | GROQ_API_KEY | local .env | READY |
| 6 | HUASHANG_API_KEY | local .env | READY |
| 7 | LOGFARE_API_KEY | local .env | READY |
| 8 | LOTTE_LIBRARY_API_KEY | local .env | READY |
| 9 | MISTRAL_API_KEY | local .env | READY |
| 10 | NVIDIA_API_KEY | local .env | READY |
| 11 | OPENCODE_API_KEY | local .env | READY |
| 12 | OPENCODE_ZEN_API_KEY | local .env | READY |
| 13 | OPENROUTER_API_KEY | local .env | READY |
| 14 | SUPACODER_API_KEY | local .env | READY |
| 15 | TOGETHER_API_KEY | local .env | READY |
| 16 | XINJIANYA_API_KEY | local .env | READY |
| 17 | ZANITY_API_KEY | local .env | READY |
| 18 | HAPUPPY_API_KEY | VPS .env | READY |
| 19 | KILO_API_KEY | VPS .env | READY |
| 20 | KILOCLOUD_API_KEY | VPS .env | READY |
| 21 | KTAI_API_KEY | VPS .env | READY |
| 22 | KTAI_PAID_API_KEY | VPS .env | READY |
| 23 | OLLAMA_CLOUD_API_KEY | VPS .env | READY |
| 24 | SWIFTROUTER_API_KEY | VPS .env | READY |
| 25 | WIWI_API_KEY | VPS .env | READY |
| 26 | BLUESMINDS_API_KEY | VPS .env | READY |
| 27 | CLAUDE_CARTER_API_KEY | VPS secrets.env | READY |
| 28 | AITOOLS_API_KEY | = KILOCLOUD_API_KEY (same key) | READY |
| 29 | ALIBABA_API_KEY | NEED FROM USER | MISSING |
| 30 | SILICONFLOW_API_KEY | NEED FROM USER | MISSING |
| 31 | TENCENT_API_KEY | NEED FROM USER | MISSING |
| 32 | XIAOMI_API_KEY | NEED FROM USER | MISSING |
| 33 | ZAI_API_KEY | NEED FROM USER | MISSING |
| 34 | ZHIPUAI_API_KEY | NEED FROM USER | MISSING |
| 35 | MINIMAX_API_KEY | NEED FROM USER | MISSING |
| 36 | KIMI_API_KEY | NEED FROM USER | MISSING |
| 37 | COHERE_API_KEY | NEED FROM USER | MISSING |
| 38 | DEEPSEEK_API_KEY | NEED FROM USER | MISSING |
| 39 | SAMBANOVA_API_KEY | NEED FROM USER | MISSING |
| 40 | POE_API_KEY | NEED FROM USER | MISSING |
| 41 | LLAMA_API_KEY | NEED FROM USER | MISSING |
| 42 | MODELSCOPE_API_KEY | NEED FROM USER | MISSING |
| 43 | CORTECS_API_KEY | NEED FROM USER | MISSING |
| 44 | CURSOR_PROXY_API_KEY | NEED FROM USER | MISSING |
| 45 | GITHUB_TOKEN | (use GITHUB_TOKEN from Actions) | READY |
| 46 | HF_API_KEY | NEED FROM USER | MISSING |
| 47 | AMAZON_NOVA_API_KEY | NEED FROM USER | MISSING |
| 48 | NANOGPT_API_KEY | NEED FROM USER | MISSING |
| 49 | KUAE_API_KEY | NEED FROM USER | MISSING |
| 50 | LLAMA_API_KEY | NEED FROM USER | MISSING |
| 51 | LLM7_API_KEY | NEED FROM USER | MISSING |
| 52 | MEGANOVA_API_KEY | NEED FROM USER | MISSING |
| 53 | MYDAMOXING_API_KEY | NEED FROM USER | MISSING |
| 54 | JIEKOU_API_KEY | NEED FROM USER | MISSING |
| 55 | ALIBABA_CN_API_KEY | = ALIBABA_API_KEY (same key) | DEP |
| 56 | SILICONFLOW_CN_API_KEY | = SILICONFLOW_API_KEY (same key) | DEP |
| 57 | TENCENT_TOKENHUB_API_KEY | = TENCENT_API_KEY (same key) | DEP |
| 58 | XIAOMI_TOKEN_PLAN_*_API_KEY (x3) | = XIAOMI_API_KEY (same key) | DEP |
| 59 | ZAI_CODING_PLAN_API_KEY | = ZAI_API_KEY (same key) | DEP |
| 60 | ZHIPUAI_CODING_PLAN_API_KEY | = ZHIPUAI_API_KEY (same key) | DEP |
| 61 | MINIMAX_CN_CODING_PLAN_API_KEY | = MINIMAX_API_KEY (same key) | DEP |
| 62 | MINIMAX_CODING_PLAN_API_KEY | = MINIMAX_API_KEY (same key) | DEP |
| 63 | KIMI_FOR_CODING_API_KEY | = KIMI_API_KEY (same key) | DEP |
| 64 | ALIBABA_CODING_PLAN_API_KEY | = ALIBABA_API_KEY (same key) | DEP |
| 65 | ALIBABA_CODING_PLAN_CN_API_KEY | = ALIBABA_API_KEY (same key) | DEP |
| 66 | GITHUB_MODELS_API_KEY | = GITHUB_TOKEN (same key) | DEP |

**DEP = derived from another secret (same key value, different env var name for opencode)**

---

## Blocked Items (Need Your Input)

1. **30+ missing API keys** — The VPS gateway `.env` only has ~47 keys. The remaining ~30 env vars (`ALIBABA_API_KEY`, `COHERE_API_KEY`, `DEEPSEEK_API_KEY`, etc.) were originally set in opencode's runtime environment but aren't stored in any file I can access. **You need to either:**
   - Provide these keys manually, OR
   - Check the VPS gateway source code (`~/LLM-API-Key-Proxy/`) for a database or config with these values, OR
   - Confirm which providers should be moved to SKIP_PROVIDERS (no key available = can't test)

2. **GITHUB_TOKEN for github-models** — The workflow already has `${{ secrets.GITHUB_TOKEN }}` from Actions. We can either:
   - Create a separate `GH_MODELS_TOKEN` secret with a PAT that has `models:read` scope, OR
   - Use the automatic `GITHUB_TOKEN` (may not have models permission by default)

---

## Execution Order (for the implementing LLM)

1. **Phase 1** — Resolve all API keys (hardest part, needs human input for ~30 keys)
2. **Phase 2** — Build final flat `.env.final` 
3. **Phase 3** — Upload secrets via `gh secret set` (scriptable, ~2 min)
4. **Phase 4** — Update workflow YAML + commit opencode.json to repo
5. **Phase 5** — Verify env var expansion works (quick test)
6. **Phase 6** — Trigger benchmark run
7. **Phase 7** — Clean up old secrets and local key files
