# llm-speedrun

Benchmark LLM providers for speed (TTFT/TPS) and quality, then optimize gateway fallback chains.

## Location

**`~/llm-speedrun/`**

If you're looking for this project, all files are in the home directory under `llm-speedrun/`.

## Quick Start

```bash
cd ~/llm-speedrun
.venv/bin/python cli.py --help
.venv/bin/python cli.py init          # Initialize SQLite database
.venv/bin/python cli.py list          # Show all targets to be tested
.venv/bin/python cli.py test          # Run speed tests (prompts for confirmation)
.venv/bin/python cli.py fetch         # Fetch external benchmark data
.venv/bin/python cli.py report        # View ranked leaderboard
.venv/bin/python cli.py apply         # Patch gateway virtual_models.yaml
```

## What it does

1. **Parses providers** from `~/.config/opencode/opencode.json` and gateway virtual models
2. **Runs streaming speed tests** measuring TTFT (time to first token) and TPS (tokens per second)
3. **Fetches benchmarks** from Aider, LiveBench, LMArena, SWE-bench
4. **Computes composite rankings** combining speed + quality
5. **Patches gateway config** at `~/LLM-API-Key-Proxy/config/virtual_models.yaml`

## Database

All results stored in SQLite at `~/llm-speedrun/data/speedrun.db`.

Tables:
- `speed_tests` — individual API call measurements
- `speed_summary` — aggregated per model/provider/effort
- `benchmarks` — external benchmark scores
- `model_aliases` — name normalization mappings

## Key Options

```bash
# Test specific provider
.venv/bin/python cli.py test --provider blazeai

# Test specific model pattern
.venv/bin/python cli.py test --model "gpt-5*"

# Include paid/credits providers
.venv/bin/python cli.py test --include-credits

# Test all reasoning effort levels (low/medium/high)
.venv/bin/python cli.py test --effort-sweep

# Adjust ranking weights
.venv/bin/python cli.py report --speed-weight 0.7 --quality-weight 0.3

# Actually write changes to gateway config
.venv/bin/python cli.py apply --write
```

## Reasoning Models

Models detected as supporting `reasoning_effort` (via pattern matching in `config.py`):
- GPT-5 family
- Claude Opus/Sonnet 4.x
- Gemini 3.x Pro
- Grok 4.x
- DeepSeek R-series
- Qwen thinking variants
- GLM 4.5/4.6/5.x
- Kimi K2.x

Default behavior tests with `reasoning_effort: "medium"`. Use `--effort-sweep` to test all levels.

## Speed measurement & fallback pipeline

The measurement math lives in one place — `benchmarks/measurement.py` — and is
consumed by both `runner.py` (the main multi-provider runner) and
`nvidia_speedtest_v2.py` (standalone speedtest), so TPS/TTFT are computed
identically everywhere. It is fully unit-tested offline (`tests/test_measurement.py`,
`tests/test_fallback.py`; the whole suite runs in under a second).

### Measurement conventions

- **TTFT** — seconds from request start to the first *content* SSE chunk
  (role-only first chunks don't count; `content` or `reasoning` deltas do).
- **TPS** — output tokens divided by the first→last token window for real
  streams. Buffered/fake streams (whole body in one burst) are detected and
  degrade to the conservative whole-call rate with `ttft_sec = 0` so a
  proxy-coalesced response cannot fabricate millions of TPS.
- **Token counting** — provider `usage` block first (`completion_tokens` /
  `output_tokens` / `tokens` / `total - prompt`), falling back to a ~4
  chars-per-token estimate. Each record says which was used in `token_source`
  (`usage` | `estimated`).
- **Records** — every call becomes a `BenchmarkRecord` (dataclass) with a UTC
  ISO-8601 `timestamp`, provider/model/base_url, status, metrics and error
  info. Serialize with `write_json()` / `write_csv()` (stable column order;
  CSV supports append for incremental runs).

### Standalone speedtest

```bash
NVIDIA_API_KEY=... python nvidia_speedtest_v2.py [--force] [--only=model-a,model-b]
BASE_URL=https://any-openai-compat/v1 python nvidia_speedtest_v2.py --only=my-model
```

Writes per-model records with timestamps to `data/nvidia_speedtest.db`
(SQLite), `data/nvidia_speedtest_results.json` and
`data/nvidia_speedtest_results.csv`.

### Fallback chain generation

Turn benchmark results into a ranked, gateway-consumable fallback chain:

```bash
python -m benchmarks.fallback data/nvidia_speedtest_results.json -o data/fallback/
python -m benchmarks.fallback results.json --min-samples 2 --max-entries 5 \
    --weight-tps 0.6 --weight-ttft 0.2 --weight-success 0.2
```

Produces `fallback_chain.yaml` + `fallback_chain.json` with this format:

```yaml
version: 1
generated_at: "2026-08-16T06:54:21+00:00"
source: results.json            # where the metrics came from
scoring:
  weights: {tps: 0.5, ttft_inv: 0.3, success: 0.2}
  min_samples: 1                # filters (provider, model) with fewer calls
  min_success_rate: 0.0
fallback_chain:                 # ranked; gateway tries priority 1 first, N+1 on failure
  - provider: fast-gpt
    model: gpt-5-mini
    priority: 1
    score: 0.91                 # weighted: tps + (1 - ttft) + success, min-max normalized
    metrics: {avg_tps: 120.5, avg_ttft_sec: 0.31, success_rate: 1.0, samples: 3}
providers:                      # per-provider aggregate view (best model wins)
  - {provider: fast-gpt, rank: 1, models: 2, best_model: gpt-5-mini, ...}
```

Score components are min-max normalized across the candidate set; TPS is
averaged over successful runs only, while failures count against
`success_rate`. Ranking is deterministic (ties break on provider/model).

### Python API

```python
from benchmarks.measurement import BenchmarkRecord, write_json, measure_openai_stream
from benchmarks.fallback import build_fallback_config, write_fallback_config

records = [...]                                   # or load_json("results.json")
config = build_fallback_config(records, source="nightly")
write_fallback_config(config, "data/fallback/")   # YAML + JSON artifacts
```
