HANDOFF CONTEXT
=============

USER REQUESTS (AS-IS)
---------------------
- "can you please find my opencode session where the agent creates code that creates an output file all_providers_benchmark_with_estimates.csv?"
- "u cant restore this session so i can resume it somehow?"
- "yeah can u run the code for me? i want data ideally for all the api providers and llms i have access to and i think most of them dont have data in the csv file. also maybe set the max_tokens for each test request higher than like 2"
- "ideally run the script so that it tries to test all the models that dont have any results yet first"
- "continue. have the benchmark run. and if it was taking too long to run, maybe analyze the code and determine if you can improve it so the code execution time is much faster"
- "do we have another api provider thats something for kilo, like kilocode gateway or something? i should have something like that" and "hapuppy isnt 100% dead either"
- "ok i recently worked on this project on a different device but i believe i committed and pushed all the changes to my github. can you investigate and ideally sync the changes with the work you've done here intelligently and non-destructively?"
- User chose to move files into llm-speedrun/ repo rather than create new repo or leave as-is
- "can u check my opencode sessions and find the project/github repo where u were working on that thing to test speeds and tps and ttft and stuff of different llms from different providers and continue working on it if theres more stuff to be done?" (current session)

GOAL
----
Continue benchmarking LLM providers, fix failure causes, add new free providers, run fresh benchmarks.

WORK COMPLETED (this session)
---------------------------
**Analysis & Fixes:**
- Analyzed 3285 test results: 310 success (9%), 2867 failures across 8 providers
- Root causes identified: xinjianya 422s (stream_options), 410 EOL models, 401 auth, 429 rate limits
- Applied runner.py fixes: xinjianya non-stream mode, temp=0.7 (not 0.0), no stream_options, no reasoning_effort
- Added smart retry: skip model_not_found, end-of-life, auth_unavailable permanently
- Added new FREE_CREDIT_PROVIDERS: freetheai, aihubmix, cortecs, opencode (from opencode.json)
- Added HIGH priority: github-models (63 free), iflowcn (14), zhipuai-coding-plan (13), zai-coding-plan (12)
- Added MEDIUM priority: alibaba/tencent/minimax/poe/groq/huggingface/siliconflow-cn coding plans
- Moved dead providers to SKIP_PROVIDERS: zenllm, swiftrouter, zenmux, llmgateway (all 401 auth)
- Added rate limits for all new providers
- Added NO_STREAM_PROVIDERS: xinjianya (fixes 39+ 422 errors)
- Added MIN_TEMPERATURE_PROVIDERS: xinjianya (rejects temp=0.0)
- Added NO_REASONING_EFFORT_PROVIDERS: xinjianya (rejects reasoning_effort on NVIDIA models)

**Git:**
- Commit 1 (b1cd974): xinjianya fixes, new providers, smarter retry (config.py + runner.py + cli.py)
- Commit 2 (26f4f2a): CLI concurrency-auto + runs default
- Commit 3 (b4c35c1): 20 more new providers (github-models, coding plans, etc.)
- Pushed to origin/main

**New Providers Status:**
- WORKING: opencode.ai/zen (16 models, ~9 tested, good results)
- NEEDS API KEY: github-models, groq, huggingface, modelscope, freetheai, iflowcn (401 errors)
- DEAD: zenllm, swiftrouter, zenmux, llmgateway (401 auth)
- DEAD: kilocloud, supacoder (all rate limited)
- LIKELY DEAD: hapuppy (needs re-test with new fixes)

**Raw Data CSV files in data/:**
- raw_tests_1d000fe7.csv: 124/1761 success (xinjianya=61, aitools=16, blazeai=15, kilo=12, nvidia=9, logfare=6, ollama-cloud=3, bluesminds=2)
- raw_tests_6418a4c5.csv: 9/9 success (nvidia provider)
- raw_tests_681d3f30.csv: 0/18 (xinjianya failures - from before fixes)
- raw_tests_0a199d75.csv: 3/3 success (nvidia)
- raw_tests_e013eb05.csv: 0/5 (huggingface - needs API key)
- raw_tests_6925cc79.csv: 0/7 (modelscope - needs API key)

PENDING TASKS
-------------
- Re-test hapuppy with new fixes (user confirmed it's not dead)
- Re-test xinjianya with non-stream mode (was all 422s before)
- Test opencode provider (16 free models, mostly working)
- Run full benchmark: `python3 cli.py test --concurrency-auto --yes --runs 1`
- Test github-models, groq, huggingface, modelscope with valid API keys
- Add EOL_MODEL_PATTERNS to skip deprecated models automatically
- Run `python3 scripts/add_estimated_time.py` after benchmarks for estimated totals
- Deduplicate outlier results (kilo had 99042 TPS - 1-token cached response)

KEY FILES
---------
- cli.py - Main CLI (test, list, csv, raw-csv, report, apply, fetch subcommands)
- config.py - Provider config, rate limits, SKIP_PROVIDERS, FREE_CREDIT_PROVIDERS, REASONING_FAMILIES
- runner.py - Streaming benchmark runner, adaptive token sizing, outlier detection
- db.py - SQLite schema (speed_tests, speed_summary tables)
- scripts/all_providers_benchmark_v2.py - Standalone probe-based benchmark (independent of CLI)
- scripts/add_estimated_time.py - Post-process CSV with estimated 10K total times
- data/all_providers_benchmark_with_estimates.csv - Full results with estimated times (780 rows)

IMPORTANT NOTES
--------------
- DB is fresh/empty - run `cli.py test` to populate
- xinjianya now uses non-stream mode with temp=0.7 (was causing 39+ 422 errors)
- `_glob_match` uses fnmatch, NOT regex - `--provider "a|b"` won't work, test one at a time
- opencode.json has 92 providers, many have "free" models but no valid API keys here
- github-models (63 free models) needs API key - all requests return 401
- groq (18 models, 5 free) also needs API key
- Best results so far: nvidia/meta/llama-3.3-70b-instruct (TTFT=0.29s, TPS=27.4), blazeai fast models
