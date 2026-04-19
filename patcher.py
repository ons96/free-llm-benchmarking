"""Patch gateway virtual_models.yaml with optimized fallback chains."""

import shutil
import time
from pathlib import Path
from typing import Optional

import yaml

from ranker import compute_rankings

GATEWAY_VM_PATH = Path.home() / "LLM-API-Key-Proxy" / "config" / "virtual_models.yaml"


def patch_gateway_config(
    speed_weight: float = 0.5,
    quality_weight: float = 0.5,
    dry_run: bool = True,
    backup: bool = True,
) -> dict:
    """
    Re-order provider→model entries in each virtual model's fallback_chain
    based on composite ranking. Only reorders existing entries; never adds/removes.
    """
    if not GATEWAY_VM_PATH.exists():
        return {"error": f"Gateway config not found at {GATEWAY_VM_PATH}"}

    data = yaml.safe_load(GATEWAY_VM_PATH.read_text())
    if not data or "virtual_models" not in data:
        return {"error": "virtual_models key missing in gateway config"}

    # Get ranked models
    ranked = compute_rankings(speed_weight, quality_weight)

    # Build lookup: (provider, model) → composite rank
    rank_lookup: dict[tuple[str, str], float] = {}
    for row in ranked:
        key = (row["provider_name"].lower(), row["model_name"].lower())
        rank_lookup[key] = row.get("composite", 0.0)

    changes = []
    vms = data["virtual_models"]

    for vm_name, vm_config in vms.items():
        chain = vm_config.get("fallback_chain")
        if not isinstance(chain, list) or not chain:
            continue

        # Score each entry
        scored = []
        for i, entry in enumerate(chain):
            if not isinstance(entry, dict):
                scored.append((i, entry, -1.0))
                continue
            provider = str(entry.get("provider", "")).lower()
            model = str(entry.get("model", "")).lower()
            score = rank_lookup.get((provider, model))
            # If no speed data, keep low score (demote)
            effective = score if score is not None else -1.0
            scored.append((i, entry, effective))

        # Sort by score desc (higher composite = better = earlier in chain)
        # Preserve original order for tied/unknown entries
        new_order = sorted(scored, key=lambda x: (-x[2], x[0]))
        old_order_repr = [
            f"{e.get('provider', '?')}:{e.get('model', '?')}"
            if isinstance(e, dict)
            else str(e)
            for _, e, _ in scored
        ]
        new_order_repr = [
            f"{e.get('provider', '?')}:{e.get('model', '?')}"
            if isinstance(e, dict)
            else str(e)
            for _, e, _ in new_order
        ]

        if old_order_repr != new_order_repr:
            # Rebuild chain with updated priorities
            new_chain = []
            for priority, (_, entry, score) in enumerate(new_order, start=1):
                if isinstance(entry, dict):
                    new_entry = dict(entry)
                    new_entry["priority"] = priority
                    new_chain.append(new_entry)
                else:
                    new_chain.append(entry)
            vm_config["fallback_chain"] = new_chain

            changes.append(
                {
                    "virtual_model": vm_name,
                    "before": old_order_repr,
                    "after": new_order_repr,
                }
            )

    result = {
        "dry_run": dry_run,
        "changes_count": len(changes),
        "changes": changes,
    }

    if not dry_run and changes:
        if backup:
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup_path = GATEWAY_VM_PATH.with_suffix(f".yaml.bak.{ts}")
            shutil.copy(GATEWAY_VM_PATH, backup_path)
            result["backup"] = str(backup_path)

        GATEWAY_VM_PATH.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        )
        result["written"] = str(GATEWAY_VM_PATH)

    return result
