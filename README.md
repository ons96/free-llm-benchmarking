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
