"""Gateway alias management for free models.

Generates stable aliases like free-best-coding, free-best-fast, free-best-long.
Updates only after N consecutive healthy probes. Keeps last-known-good fallback.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from freeradar.registry import (
    connect,
    get_alias,
    list_aliases,
    set_alias,
    alias_history,
    ALIASES,
    HEALTH_HEALTHY,
)
from freeradar.rank import rank_models, RankedModel

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_VIRTUAL_PATH = Path.home() / "LLM-API-Key-Proxy" / "config" / "virtual_models.yaml"

MIN_CONSECUTIVE_HEALTHY = 3  # Promote only after N consecutive healthy probes
MAX_ALIAS_HISTORY = 100


@dataclass
class AliasProposal:
    alias: str
    task: str
    current_provider: str | None
    current_model: str | None
    proposed_provider: str | None
    proposed_model: str | None
    score: float | None
    healthy_streak: int
    fallback_provider: str | None
    fallback_model: str | None
    action: str  # "promote", "demote", "keep", "init"
    reason: str


def _read_gateway_yaml() -> dict:
    if not GATEWAY_VIRTUAL_PATH.exists():
        return {"virtual_models": {}}
    return yaml.safe_load(GATEWAY_VIRTUAL_PATH.read_text()) or {"virtual_models": {}}


def _write_gateway_yaml(data: dict, backup: bool = True) -> Path | None:
    backup_path = None
    if backup and GATEWAY_VIRTUAL_PATH.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = GATEWAY_VIRTUAL_PATH.with_suffix(f".yaml.bak.{ts}")
        shutil.copy(GATEWAY_VIRTUAL_PATH, backup_path)

    if GATEWAY_VIRTUAL_PATH.exists():
        GATEWAY_VIRTUAL_PATH.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    return backup_path


def compute_alias_proposals(
    db_path: Path | None = None,
    min_streak: int = MIN_CONSECUTIVE_HEALTHY,
) -> list[AliasProposal]:
    """Compute proposed alias changes based on current rankings."""
    proposals = []

    for alias, task in ALIASES.items():
        # Get current alias state
        conn = connect(db_path)
        current = get_alias(conn, alias)
        conn.close()

        current_provider = current.get("provider") if current else None
        current_model = current.get("model_id") if current else None
        current_streak = current.get("healthy_streak", 0) if current else 0
        fallback_provider = current.get("fallback_provider") if current else None
        fallback_model = current.get("fallback_model_id") if current else None

        # Get ranked models for this task
        ranked = rank_models(task, top_n=5, db_path=db_path)

        if not ranked:
            proposals.append(AliasProposal(
                alias=alias,
                task=task,
                current_provider=current_provider,
                current_model=current_model,
                proposed_provider=None,
                proposed_model=None,
                score=None,
                healthy_streak=current_streak,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
                action="keep",
                reason="no eligible models",
            ))
            continue

        best = ranked[0]
        second_best = ranked[1] if len(ranked) > 1 else None

        # Check if best model meets promotion criteria
        if best.consecutive_healthy >= min_streak and best.health == HEALTH_HEALTHY:
            if current_provider != best.provider or current_model != best.model_id:
                # Promote new best
                new_fallback_provider = current_provider
                new_fallback_model = current_model
                if not new_fallback_provider and second_best:
                    new_fallback_provider = second_best.provider
                    new_fallback_model = second_best.model_id

                proposals.append(AliasProposal(
                    alias=alias,
                    task=task,
                    current_provider=current_provider,
                    current_model=current_model,
                    proposed_provider=best.provider,
                    proposed_model=best.model_id,
                    score=best.score,
                    healthy_streak=best.consecutive_healthy,
                    fallback_provider=new_fallback_provider,
                    fallback_model=new_fallback_model,
                    action="promote",
                    reason=f"new best with streak={best.consecutive_healthy}, score={best.score:.3f}",
                ))
            else:
                # Already the best, update streak
                proposals.append(AliasProposal(
                    alias=alias,
                    task=task,
                    current_provider=current_provider,
                    current_model=current_model,
                    proposed_provider=current_provider,
                    proposed_model=current_model,
                    score=best.score,
                    healthy_streak=best.consecutive_healthy,
                    fallback_provider=fallback_provider,
                    fallback_model=fallback_model,
                    action="keep",
                    reason=f"current best streak={best.consecutive_healthy}",
                ))
        else:
            # Best doesn't meet streak requirement
            if current_provider and current_model:
                # Keep current, but check if it's still healthy
                current_healthy = any(
                    r.provider == current_provider and r.model_id == current_model and r.health == HEALTH_HEALTHY
                    for r in ranked
                )
                if not current_healthy and second_best and second_best.consecutive_healthy >= min_streak:
                    # Demote to second best
                    proposals.append(AliasProposal(
                        alias=alias,
                        task=task,
                        current_provider=current_provider,
                        current_model=current_model,
                        proposed_provider=second_best.provider,
                        proposed_model=second_best.model_id,
                        score=second_best.score,
                        healthy_streak=second_best.consecutive_healthy,
                        fallback_provider=current_provider,
                        fallback_model=current_model,
                        action="demote",
                        reason=f"current unhealthy, fallback to streak={second_best.consecutive_healthy}",
                    ))
                else:
                    proposals.append(AliasProposal(
                        alias=alias,
                        task=task,
                        current_provider=current_provider,
                        current_model=current_model,
                        proposed_provider=current_provider,
                        proposed_model=current_model,
                        score=next((r.score for r in ranked if r.provider == current_provider and r.model_id == current_model), None),
                        healthy_streak=current_streak,
                        fallback_provider=fallback_provider,
                        fallback_model=fallback_model,
                        action="keep",
                        reason=f"waiting for streak (best={best.consecutive_healthy}, min={min_streak})",
                    ))
            else:
                # No current alias, initialize with best if eligible
                if best.consecutive_healthy >= min_streak:
                    proposals.append(AliasProposal(
                        alias=alias,
                        task=task,
                        current_provider=None,
                        current_model=None,
                        proposed_provider=best.provider,
                        proposed_model=best.model_id,
                        score=best.score,
                        healthy_streak=best.consecutive_healthy,
                        fallback_provider=second_best.provider if second_best else None,
                        fallback_model=second_best.model_id if second_best else None,
                        action="init",
                        reason=f"initial promotion streak={best.consecutive_healthy}",
                    ))
                else:
                    proposals.append(AliasProposal(
                        alias=alias,
                        task=task,
                        current_provider=None,
                        current_model=None,
                        proposed_provider=None,
                        proposed_model=None,
                        score=None,
                        healthy_streak=0,
                        fallback_provider=None,
                        fallback_model=None,
                        action="keep",
                        reason=f"no model meets streak requirement (best={best.consecutive_healthy})",
                    ))

    return proposals


def apply_alias_proposals(
    proposals: list[AliasProposal],
    db_path: Path | None = None,
    dry_run: bool = True,
    backup: bool = True,
) -> dict[str, Any]:
    """Apply alias proposals to registry and gateway YAML."""
    changes = []
    gateway_data = _read_gateway_yaml()
    vm = gateway_data.setdefault("virtual_models", {})

    for p in proposals:
        if p.action in ("promote", "demote", "init") and p.proposed_provider and p.proposed_model:
            # Update registry
            if not dry_run:
                conn = connect(db_path)
                set_alias(
                    conn,
                    p.alias,
                    p.task,
                    p.proposed_provider,
                    p.proposed_model,
                    p.score,
                    p.healthy_streak,
                    p.fallback_provider,
                    p.fallback_model,
                    action=p.action,
                )
                conn.commit()
                conn.close()

            # Update gateway YAML
            chain = []
            if p.proposed_provider and p.proposed_model:
                chain.append({"provider": p.proposed_provider, "model": p.proposed_model, "priority": 1})
            if p.fallback_provider and p.fallback_model:
                chain.append({"provider": p.fallback_provider, "model": p.fallback_model, "priority": 2})

            vm[p.alias] = {
                "description": f"Auto-managed by free-model radar ({p.task})",
                "fallback_chain": chain,
                "settings": {},
            }
            changes.append({
                "alias": p.alias,
                "action": p.action,
                "from": f"{p.current_provider}/{p.current_model}" if p.current_provider else "none",
                "to": f"{p.proposed_provider}/{p.proposed_model}",
                "score": p.score,
                "reason": p.reason,
            })
        elif p.action == "keep" and p.current_provider and p.current_model:
            # Ensure gateway has current alias
            if p.alias not in vm:
                chain = [{"provider": p.current_provider, "model": p.current_model, "priority": 1}]
                if p.fallback_provider and p.fallback_model:
                    chain.append({"provider": p.fallback_provider, "model": p.fallback_model, "priority": 2})
                vm[p.alias] = {
                    "description": f"Auto-managed by free-model radar ({p.task})",
                    "fallback_chain": chain,
                    "settings": {},
                }

    backup_path = None
    if not dry_run:
        backup_path = _write_gateway_yaml(gateway_data, backup)

    return {
        "applied": not dry_run,
        "changes": changes,
        "backup": str(backup_path) if backup_path else None,
        "gateway_path": str(GATEWAY_VIRTUAL_PATH),
    }


def get_alias_status(db_path: Path | None = None) -> dict[str, Any]:
    """Get current alias status for dashboard."""
    conn = connect(db_path)
    aliases = list_aliases(conn)

    status = {}
    for a in aliases:
        alias = a["alias"]
        history = alias_history(conn, alias, limit=10)
        status[alias] = {
            "task": a.get("task"),
            "provider": a.get("provider"),
            "model": a.get("model_id"),
            "score": a.get("score"),
            "updated_at": a.get("updated_at"),
            "healthy_streak": a.get("healthy_streak", 0),
            "fallback_provider": a.get("fallback_provider"),
            "fallback_model": a.get("fallback_model_id"),
            "history": history,
        }
    conn.close()
    return status


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Manage gateway aliases for free models")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Show proposed changes without applying")
    parser.add_argument("--apply", dest="dry_run", action="store_false", help="Apply changes to gateway")
    parser.add_argument("--no-backup", dest="backup", action="store_false", help="Skip backup")
    parser.add_argument("--min-streak", type=int, default=MIN_CONSECUTIVE_HEALTHY, help="Min consecutive healthy probes")
    parser.add_argument("--db", type=Path, help="Database path")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    proposals = compute_alias_proposals(args.db, args.min_streak)

    if args.json:
        print(json.dumps([vars(p) for p in proposals], indent=2, default=str))
    else:
        print(f"\n=== Alias Proposals (dry_run={args.dry_run}) ===")
        for p in proposals:
            print(f"\n  {p.alias} ({p.task})")
            print(f"    Action: {p.action}")
            print(f"    Current: {p.current_provider}/{p.current_model}" if p.current_provider else "    Current: none")
            print(f"    Proposed: {p.proposed_provider}/{p.proposed_model}" if p.proposed_provider else "    Proposed: none")
            print(f"    Score: {p.score:.3f}" if p.score else "    Score: N/A")
            print(f"    Streak: {p.healthy_streak}")
            print(f"    Fallback: {p.fallback_provider}/{p.fallback_model}" if p.fallback_provider else "    Fallback: none")
            print(f"    Reason: {p.reason}")

    if not args.dry_run:
        result = apply_alias_proposals(proposals, args.db, dry_run=False, backup=args.backup)
        print(f"\n=== Applied {len(result['changes'])} changes ===")
        if result["backup"]:
            print(f"Backup: {result['backup']}")
        if args.json:
            print(json.dumps(result, indent=2, default=str))

    sys.exit(0)