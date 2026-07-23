"""Tests for registry-driven provider credit classification (#413).

Regression guard for two bugs found 2026-07-22:
  1. New recurring providers (qzz/gratisfy/tokenlb) were skipped by the
     default free-filter, never entering the nightly speedrun.
  2. `expensive` was bound only inside the FINITE_CREDIT_PROVIDERS branch of
     load_opencode_targets but read unconditionally at Target(), so every
     recurring/unlimited provider raised NameError and crashed target load.

Both are structural, not network-dependent, so we assert on the derived sets
and on the source of load_opencode_targets rather than making live calls.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


def test_registry_loaded_nonempty():
    # If the registry file is present it must classify a meaningful number of
    # providers; empty sets mean the JSON failed to load (silent degrade).
    assert config._REGISTRY_PROVIDERS, "provider_registry.json failed to load"
    assert len(config.RECURRING_PROVIDERS) > 10
    assert len(config.FINITE_CREDIT_PROVIDERS) > 0
    assert len(config.DEAD_PROVIDERS) > 0


def test_new_providers_are_recurring_not_finite():
    # #413: these must be classed recurring so the default sweep tests them.
    for p in ("qzz", "gratisfy", "tokenlb"):
        assert p in config.RECURRING_PROVIDERS, f"{p} not recurring"
        assert p not in config.FINITE_CREDIT_PROVIDERS, f"{p} wrongly finite"
        assert p not in config.DEAD_PROVIDERS, f"{p} wrongly dead"


def test_credit_type_sets_are_disjoint():
    # A provider must not land in two credit buckets.
    assert not (config.RECURRING_PROVIDERS & config.FINITE_CREDIT_PROVIDERS)
    assert not (config.RECURRING_PROVIDERS & config.DEAD_PROVIDERS)
    assert not (config.FINITE_CREDIT_PROVIDERS & config.DEAD_PROVIDERS)


def test_expensive_bound_before_use():
    # Regression: `expensive` must be assigned before its first read in
    # load_opencode_targets, else recurring providers NameError.
    tree = ast.parse((Path(config.__file__)).read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "load_opencode_targets"
    )
    assigns = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == "expensive"]
    uses = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == "expensive"]
    assert assigns and uses
    assert min(assigns) < min(uses), "expensive read before it is bound"


if __name__ == "__main__":
    test_registry_loaded_nonempty()
    test_new_providers_are_recurring_not_finite()
    test_credit_type_sets_are_disjoint()
    test_expensive_bound_before_use()
    print("registry credit tests: OK")
