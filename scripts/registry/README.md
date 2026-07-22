# Provider Credit Type Registry (v1.1)

`data/provider_registry.json` classifies each opencode provider by credit_type:
- `unlimited_free` — true unlimited free tier
- `recurring_auto_refresh` — daily/auto-refreshing quota, no user action
- `recurring_checkin_required` — requires daily/periodic user check-in
- `finite` — prepaid credit drains to zero
- `dead` — host down or key dead
- `unknown` — no docs found; defaults to finite for test budgets

## Scripts
- `build-registry-v1.1.py` — initial builder from `config/opencode-runner.json` + AAAK evidence
- `merge-classified.py` — applies CONFIDENT dict of websearch-confirmed classifications to the registry in place

## Rebuild
```bash
python3 scripts/registry/build-registry-v1.1.py
python3 scripts/registry/merge-classified.py
```

Counts (2026-07-22): dead=11, finite=24, recurring_auto_refresh=62, recurring_checkin_required=7, unknown=10 (7 non-LLM skipped + 3 truly-unknown LLM: buddybackend, redwakeai, yanproxy), unlimited_free=12. Total 126 providers.
