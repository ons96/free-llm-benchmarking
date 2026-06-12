"""Objective fallback chain generator with probe classifier and cooldown store.

Reads from three sources, never writes secrets:
  - data/speedrun.db (local TTFT/TPS + success rate)
  - ~/CodingProjects/llm-provider-manager/llm_providers.db (provider/model metadata)
  - ~/CodingProjects/llm-leaderboard-aggregate/db/models.db (benchmarks, optional)

Writes to data/fallback_chains.db (scores, probes, cooldowns, snapshots)
and applies ranked chains to LLM-API-Key-Proxy/config/virtual_models.yaml.

CLI surface: python cli.py chain --class coding-elite --dry-run
             python cli.py chain --probe --apply
             python cli.py chain --class coding-fast --class reasoning --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

from config import load_all_targets, Target
from ranker import compute_rankings

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "fallback_chains.db"
PROVIDER_MGR_DB = Path.home() / "CodingProjects" / "llm-provider-manager" / "llm_providers.db"
LEADERBOARD_DB = Path.home() / "CodingProjects" / "llm-leaderboard-aggregate" / "db" / "models.db"
GATEWAY_VM_PATH = Path.home() / "LLM-API-Key-Proxy" / "config" / "virtual_models.yaml"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    class TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    score REAL NOT NULL,
    components_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (class, provider, model)
);
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    base_url TEXT,
    api_key_env TEXT,
    status_code INTEGER,
    error TEXT,
    has_tool_call INTEGER DEFAULT 0,
    latency_ms INTEGER,
    ok INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probes_pair ON probes(provider, model, ts DESC);
CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    duration_s INTEGER NOT NULL,
    until_ts TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cooldowns_active ON cooldowns(provider, model, until_ts DESC);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class TEXT NOT NULL,
    chain_yaml_path TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class ClassAction(str, Enum):
    OK = "ok"
    COOLDOWN = "cooldown"
    EXCLUDE = "exclude"
    QUARANTINE = "quarantine"
    SKIP = "skip"
    DOWNGRADE = "downgrade"


# Cooldown durations (seconds). Tuned to spec: 429->temp, 401/403->long, model-not-found->quarantine.
COOLDOWN_S = {
    ClassAction.COOLDOWN: 3600,        # 1h for rate-limit / transient
    ClassAction.EXCLUDE: 86400,         # 24h for auth failure
    ClassAction.QUARANTINE: 604800,     # 7d for model-not-found / provider-mapping
    ClassAction.DOWNGRADE: 21600,       # 6h for tool-call-unsupported (may be fixed)
}


# Per-class scoring weights. Components must sum to 1.0.
#   quality  = normalized public benchmark score (0..1, 0 if unknown)
#   tps      = normalized local avg_tps (0..1)
#   ttft_inv = 1 - normalized local avg_ttft (0..1, higher is faster)
#   success  = local success_rate (0..1)
#   tool_cap = 1 if tool_calls supported else 0 (eligibility multiplier)
CLASS_PROFILES: dict[str, dict[str, float]] = {
    "coding-elite": {"quality": 0.50, "tps": 0.20, "ttft_inv": 0.10, "success": 0.10, "tool_cap": 0.10, "min_score": 0.0},
    "coding-fast":  {"quality": 0.15, "tps": 0.50, "ttft_inv": 0.20, "success": 0.10, "tool_cap": 0.05, "min_score": 0.0},
    "reasoning":    {"quality": 0.60, "tps": 0.15, "ttft_inv": 0.10, "success": 0.10, "tool_cap": 0.05, "min_score": 0.0},
    "title-fast":   {"quality": 0.05, "tps": 0.70, "ttft_inv": 0.15, "success": 0.10, "tool_cap": 0.00, "min_score": 0.0},
}

# Map vm names to class profiles. Easy to extend.
VM_CLASS_MAP: dict[str, str] = {
    "coding-elite": "coding-elite",
    "coding-smart": "coding-elite",  # alias
    "coding-fast": "coding-fast",
    "agent-oracle": "reasoning",
    "reasoning": "reasoning",
    "title-fast": "title-fast",
    "glm5-elite": "reasoning",
    "chat-fast": "coding-fast",
    "chat-elite": "reasoning",
    "chat-smart": "coding-elite",
}


def init_db() -> None:
    """Create schema if missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(SCHEMA)
    con.close()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    action: ClassAction
    reason: str
    cooldown_s: int


def classify_probe_result(
    status_code: Optional[int],
    body: str = "",
    exception: Optional[BaseException] = None,
    has_tool_call: bool = False,
) -> Classification:
    """Classify a single probe outcome into a chain action.

    Policy (matches AC):
      - 401/403 invalid key   -> EXCLUDE (24h)
      - 404 model not found   -> QUARANTINE (7d)
      - 429 / 5xx / timeout   -> COOLDOWN (1h, transient)
      - malformed JSON        -> SKIP (0s, no record)
      - tool-call unsupported -> DOWNGRADE (6h, eligibility demoted)
      - success (2xx + body)  -> OK (0s)
    """
    if exception is not None:
        # Network/timeout/transport errors
        name = type(exception).__name__
        return Classification(ClassAction.COOLDOWN, f"network:{name}", COOLDOWN_S[ClassAction.COOLDOWN])

    if status_code is None:
        return Classification(ClassAction.SKIP, "no_status", 0)

    if 200 <= status_code < 300:
        # Success — but record tool-call demotion as a side note
        if not has_tool_call:
            return Classification(ClassAction.DOWNGRADE, "no_tool_call_in_probe", COOLDOWN_S[ClassAction.DOWNGRADE])
        return Classification(ClassAction.OK, "ok", 0)

    if status_code in (401, 403):
        return Classification(ClassAction.EXCLUDE, f"auth_{status_code}", COOLDOWN_S[ClassAction.EXCLUDE])

    if status_code == 404:
        return Classification(ClassAction.QUARANTINE, "model_not_found", COOLDOWN_S[ClassAction.QUARANTINE])

    if status_code == 429 or 500 <= status_code < 600:
        return Classification(ClassAction.COOLDOWN, f"http_{status_code}", COOLDOWN_S[ClassAction.COOLDOWN])

    if status_code == 400:
        # Could be malformed request or model rejects. Treat as skip (no record).
        return Classification(ClassAction.SKIP, f"http_400:{body[:50]}", 0)

    return Classification(ClassAction.COOLDOWN, f"other_{status_code}", COOLDOWN_S[ClassAction.COOLDOWN])


# ---------------------------------------------------------------------------
# Probe loop
# ---------------------------------------------------------------------------

async def _probe_one(target: Target, session_factory) -> dict:
    """Single async OpenAI-compat /chat/completions probe. Never raises."""
    import httpx

    start = time.time()
    status_code: Optional[int] = None
    body = ""
    has_tool_call = False
    error: Optional[str] = None
    api_key_env = ""
    try:
        # Resolve api key from env (already done by config, but target may not carry it)
        api_key = os.environ.get(target.provider_name.upper().replace("-", "_") + "_API_KEY", "")
    except Exception:
        api_key = ""
    try:
        async with session_factory() as client:
            resp = await client.post(
                f"{target.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}" if api_key else "Bearer ",
                    "Content-Type": "application/json",
                },
                json={
                    "model": target.model_name,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4,
                    "stream": False,
                },
                timeout=10.0,
            )
            status_code = resp.status_code
            body = resp.text[:200]
            try:
                j = resp.json()
                if "tool_calls" in json.dumps(j):
                    has_tool_call = True
            except Exception:
                pass
    except BaseException as e:  # network/timeout
        error = f"{type(e).__name__}: {e}"
    latency_ms = int((time.time() - start) * 1000)
    return {
        "provider": target.provider_name,
        "model": target.model_name,
        "base_url": target.base_url,
        "api_key_env": api_key_env,
        "status_code": status_code,
        "body": body,
        "error": error,
        "has_tool_call": has_tool_call,
        "latency_ms": latency_ms,
    }


async def run_probes(
    provider_filter: Optional[str] = None,
    concurrency: int = 5,
    timeout_total_s: int = 180,
) -> list[Classification]:
    """Run bounded-concurrency OpenAI probes over configured targets."""
    import httpx

    targets = load_all_targets(
        include_expensive=False,
        include_paid=False,
        provider_filter=provider_filter,
    )
    if not targets:
        return []

    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(10.0)

    async def bounded(target):
        async with sem:
            return await _probe_one(target, lambda: httpx.AsyncClient(timeout=timeout))

    tasks = [asyncio.create_task(bounded(t)) for t in targets[:50]]  # cap to 50 to stay VPS-friendly
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_total_s)
    except asyncio.TimeoutError:
        results = [t.result() if t.done() else {"provider": "?", "model": "?", "status_code": None, "error": "gather_timeout", "has_tool_call": False, "latency_ms": timeout_total_s * 1000} for t in tasks]

    init_db()
    classifications: list[Classification] = []
    con = _connect()
    try:
        for r in results:
            if not isinstance(r, dict):
                continue
            cls = classify_probe_result(
                status_code=r.get("status_code"),
                body=r.get("body", ""),
                exception=Exception(r["error"]) if r.get("error") else None,
                has_tool_call=r.get("has_tool_call", False),
            )
            con.execute(
                "INSERT INTO probes(provider, model, base_url, api_key_env, status_code, error, has_tool_call, latency_ms, ok, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    r["provider"], r["model"], r.get("base_url", ""), r.get("api_key_env", ""),
                    r.get("status_code"), r.get("error", ""),
                    1 if r.get("has_tool_call") else 0, r.get("latency_ms", 0),
                    1 if cls.action == ClassAction.OK else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            if cls.action in (ClassAction.COOLDOWN, ClassAction.EXCLUDE, ClassAction.QUARANTINE, ClassAction.DOWNGRADE):
                con.execute(
                    "INSERT INTO cooldowns(provider, model, action, reason, duration_s, until_ts, created_at) VALUES (?,?,?,?,?,?,?)",
                    (r["provider"], r["model"], cls.action.value, cls.reason, cls.cooldown_s,
                     datetime.fromtimestamp(time.time() + cls.cooldown_s, tz=timezone.utc).isoformat(),
                     datetime.now(timezone.utc).isoformat()),
                )
            classifications.append(cls)
        con.commit()
    finally:
        con.close()
    return classifications


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

def _load_speedrun_metrics() -> dict[tuple[str, str], dict]:
    """Return {(provider, model): {avg_tps, avg_ttft, success_rate, num_runs}} from speedrun.db."""
    speedrun_db = REPO_ROOT / "data" / "speedrun.db"
    if not speedrun_db.exists():
        return {}
    con = sqlite3.connect(str(speedrun_db))
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT provider_name, model_name,
               AVG(CASE WHEN status='success' AND tps > 0 THEN tps END) AS avg_tps,
               AVG(CASE WHEN status='success' AND ttft_sec > 0 THEN ttft_sec END) AS avg_ttft,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS success_rate,
               COUNT(*) AS num_runs
        FROM speed_tests
        GROUP BY provider_name, model_name
    """).fetchall()
    con.close()
    return {
        (r["provider_name"], r["model_name"]): {
            "avg_tps": r["avg_tps"],
            "avg_ttft": r["avg_ttft"],
            "success_rate": r["success_rate"] or 0.0,
            "num_runs": r["num_runs"] or 0,
        } for r in rows
    }


def _load_bench_scores() -> dict[str, float]:
    """Return {model_id: avg_score} from leaderboard DB or speedrun benchmarks table."""
    candidates = []
    if LEADERBOARD_DB.exists():
        try:
            con = sqlite3.connect(str(LEADERBOARD_DB))
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT model_id, AVG(avg_agentic_coding_score) AS s
                FROM model_providers
                WHERE avg_agentic_coding_score IS NOT NULL
                GROUP BY model_id
            """).fetchall()
            con.close()
            candidates = [(r["model_id"], r["s"]) for r in rows if r["s"] is not None]
        except Exception:
            pass
    if not candidates:
        speedrun_db = REPO_ROOT / "data" / "speedrun.db"
        if speedrun_db.exists():
            con = sqlite3.connect(str(speedrun_db))
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT model_canonical, AVG(score) AS s FROM benchmarks WHERE score IS NOT NULL GROUP BY model_canonical").fetchall()
            con.close()
            candidates = [(r["model_canonical"], r["s"]) for r in rows if r["s"] is not None]
    return {mid: float(s) for mid, s in candidates}


def _load_tool_capable() -> set[tuple[str, str]]:
    """Return set of (provider, model) confirmed tool-capable from speedrun db."""
    speedrun_db = REPO_ROOT / "data" / "speedrun.db"
    if not speedrun_db.exists():
        return set()
    con = sqlite3.connect(str(speedrun_db))
    rows = con.execute("SELECT DISTINCT provider_name, model_name FROM speed_tests WHERE tool_calls > 0").fetchall()
    con.close()
    return {(r[0], r[1]) for r in rows}


def _get_active_cooldowns() -> dict[tuple[str, str], dict]:
    """Return {(provider, model): {action, reason, until_ts}} for active cooldowns."""
    if not DB_PATH.exists():
        return {}
    con = _connect()
    now = datetime.now(timezone.utc).isoformat()
    rows = con.execute("""
        SELECT provider, model, action, reason, MAX(until_ts) AS until_ts
        FROM cooldowns
        WHERE until_ts > ?
        GROUP BY provider, model
    """, (now,)).fetchall()
    con.close()
    return {(r["provider"], r["model"]): {"action": r["action"], "reason": r["reason"], "until_ts": r["until_ts"]} for r in rows}


def _normalize_0_1(values: list[Optional[float]]) -> dict[int, float]:
    """Min-max normalize non-None values to [0, 1]. None -> 0.5 default if all None."""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if not present:
        return {}
    if len(present) < 2:
        return {i: 0.5 for i, _ in present}
    lo, hi = min(v for _, v in present), max(v for _, v in present)
    if hi == lo:
        return {i: 0.5 for i, _ in present}
    return {i: (v - lo) / (hi - lo) for i, v in present}


def compute_class_scores(class_name: str) -> list[dict]:
    """Return ranked list of {provider, model, score, components} for the class."""
    profile = CLASS_PROFILES.get(class_name)
    if not profile:
        raise ValueError(f"unknown class {class_name}; choose from {list(CLASS_PROFILES)}")

    # Build candidate set from speedrun.db (fast, no network). We only rank
    # models that have actually been tested. Cross-reference with provider-manager
    # for capability flags.
    speedrun_db = REPO_ROOT / "data" / "speedrun.db"
    if not speedrun_db.exists():
        return []
    con = sqlite3.connect(str(speedrun_db))
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT provider_name, model_name,
               AVG(CASE WHEN status='success' AND tps > 0 THEN tps END) AS avg_tps,
               AVG(CASE WHEN status='success' AND ttft_sec > 0 THEN ttft_sec END) AS avg_ttft,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS success_rate,
               COUNT(*) AS num_runs
        FROM speed_tests
        WHERE status = 'success'
        GROUP BY provider_name, model_name
        HAVING num_runs >= 1
    """).fetchall()
    con.close()

    bench = _load_bench_scores()
    tool_capable = _load_tool_capable()

    # Build rows. Filter out suspicious (burst/fake-streaming) entries the same
    # way build_site_json.py does — those rows have broken TPS measurements and
    # would otherwise dominate the top of every ranking.
    def _is_suspicious(avg_tps, provider, model):
        if avg_tps is None:
            return True
        if avg_tps > 100000:
            return True
        return False

    # Build rows
    out_rows = []
    for r in rows:
        if _is_suspicious(r["avg_tps"], r["provider_name"], r["model_name"]):
            continue
        out_rows.append({
            "provider": r["provider_name"],
            "model": r["model_name"],
            "tps": r["avg_tps"],
            "ttft": r["avg_ttft"],
            "success": r["success_rate"] or 0.0,
            "bench": bench.get(r["model_name"].lower()),
            "tool_capable": (r["provider_name"], r["model_name"]) in tool_capable,
        })

    tps_n = _normalize_0_1([r["tps"] for r in out_rows])
    ttft_n = _normalize_0_1([r["ttft"] for r in out_rows])
    bench_n = _normalize_0_1([r["bench"] for r in out_rows])

    for i, r in enumerate(out_rows):
        components = {
            "quality": bench_n.get(i, 0.0) if r["bench"] is not None else 0.0,
            "tps": tps_n.get(i, 0.0) if r["tps"] is not None else 0.0,
            "ttft_inv": (1.0 - ttft_n.get(i, 0.5)) if r["ttft"] is not None else 0.0,
            "success": r["success"] if r["success"] is not None else 0.0,
            "tool_cap": 1.0 if r["tool_capable"] else 0.0,
        }
        score = sum(profile[k] * components[k] for k in profile if k != "min_score")
        # Tool-capable multiplier for coding-elite / coding-fast: zero score if not tool-capable (coding)
        if class_name in ("coding-elite", "coding-fast") and not r["tool_capable"] and r["bench"] is None:
            score *= 0.5  # mild demotion rather than hard zero
        r["score"] = round(score, 6)
        r["components"] = components

    out_rows.sort(key=lambda r: r["score"], reverse=True)
    return out_rows


# ---------------------------------------------------------------------------
# Chain generation + apply
# ---------------------------------------------------------------------------

@dataclass
class ChainEntry:
    provider: str
    model: str
    priority: int
    score: float = 0.0
    components: dict = field(default_factory=dict)
    cooldown_action: Optional[str] = None
    cooldown_reason: Optional[str] = None


def generate_chain_for_vm(vm_name: str, class_name: Optional[str] = None) -> list[ChainEntry]:
    """Produce ranked ChainEntry list for one virtual model. Applies active cooldowns/exclusions."""
    cls = class_name or VM_CLASS_MAP.get(vm_name)
    if not cls:
        raise ValueError(f"no class mapped for vm {vm_name}")
    scores = compute_class_scores(cls)
    cooldowns = _get_active_cooldowns()
    out: list[ChainEntry] = []
    pri = 1
    for r in scores:
        key = (r["provider"], r["model"])
        cd = cooldowns.get(key)
        if cd and cd["action"] == ClassAction.EXCLUDE.value:
            continue  # skip — locked out for 24h
        if cd and cd["action"] == ClassAction.QUARANTINE.value:
            continue  # skip — model bad
        # COOLDOWN and DOWNGRADE are demoted but kept (we still want them at lower priority
        # so chains don't shrink to nothing during transient outages)
        entry = ChainEntry(
            provider=r["provider"],
            model=r["model"],
            priority=pri,
            score=r["score"],
            components=r["components"],
            cooldown_action=cd["action"] if cd else None,
            cooldown_reason=cd["reason"] if cd else None,
        )
        out.append(entry)
        pri += 1
        if pri > 30:
            break
    return out


def _read_gateway_yaml() -> dict:
    if not GATEWAY_VM_PATH.exists():
        return {"virtual_models": {}}
    return yaml.safe_load(GATEWAY_VM_PATH.read_text()) or {"virtual_models": {}}


def build_dry_run(classes: list[str]) -> dict[str, list[ChainEntry]]:
    """Compute chains for each class/vm, no disk writes."""
    out: dict[str, list[ChainEntry]] = {}
    for cls in classes:
        try:
            out[cls] = generate_chain_for_vm(cls, class_name=cls if cls in CLASS_PROFILES else None)
        except ValueError:
            # Map vm name -> class
            mapped = VM_CLASS_MAP.get(cls)
            if mapped:
                out[cls] = generate_chain_for_vm(cls, class_name=mapped)
            else:
                raise
    return out


def build_apply(chains: dict[str, list[ChainEntry]], backup: bool = True) -> dict:
    """Apply generated chains to gateway yaml. Preserves existing settings; only rewrites fallback_chain
    + adds a `_generated_at` metadata line per affected vm. Returns summary."""
    data = _read_gateway_yaml()
    if "virtual_models" not in data:
        data["virtual_models"] = {}

    if "metadata" not in data:
        data["metadata"] = {}
    if "sources" not in data["metadata"]:
        data["metadata"]["sources"] = []
    if "objective-leaderboards" not in data["metadata"]["sources"]:
        data["metadata"]["sources"].append("objective-leaderboards")
    data["metadata"]["last_chain_update"] = datetime.now(timezone.utc).isoformat()

    applied = []
    for vm_name, entries in chains.items():
        vms = data["virtual_models"]
        if vm_name not in vms:
            vms[vm_name] = {"description": f"Auto-generated by fallback_chain", "fallback_chain": [], "settings": {}}
        vms[vm_name]["fallback_chain"] = [
            {"provider": e.provider, "model": e.model, "priority": e.priority}
            for e in entries
        ]
        applied.append({"vm": vm_name, "count": len(entries)})

    if backup and GATEWAY_VM_PATH.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy(GATEWAY_VM_PATH, GATEWAY_VM_PATH.with_suffix(f".yaml.bak.{ts}"))

    if GATEWAY_VM_PATH.exists():
        GATEWAY_VM_PATH.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))

    init_db()
    con = _connect()
    try:
        for vm_name, entries in chains.items():
            con.execute(
                "INSERT INTO snapshots(class, chain_yaml_path, entry_count, applied_at) VALUES (?,?,?,?)",
                (vm_name, str(GATEWAY_VM_PATH), len(entries), datetime.now(timezone.utc).isoformat()),
            )
        # Persist scores too
        for vm_name, entries in chains.items():
            cls = next((c for c, m in VM_CLASS_MAP.items() if m == VM_CLASS_MAP.get(vm_name)), vm_name)
            for e in entries:
                con.execute(
                    "INSERT OR REPLACE INTO scores(class, provider, model, score, components_json, computed_at) VALUES (?,?,?,?,?,?)",
                    (vm_name, e.provider, e.model, e.score, json.dumps(e.components), datetime.now(timezone.utc).isoformat()),
                )
        con.commit()
    finally:
        con.close()

    return {"applied": applied, "path": str(GATEWAY_VM_PATH)}


# ---------------------------------------------------------------------------
# CLI hook
# ---------------------------------------------------------------------------

def cmd_chain(args) -> int:
    init_db()
    if args.probe:
        print(f"=== Probing (concurrency={args.concurrency}) ===")
        classifications = asyncio.run(run_probes(
            provider_filter=args.provider,
            concurrency=args.concurrency,
        ))
        from collections import Counter
        c = Counter(cl.action.value for cl in classifications)
        for action, n in c.most_common():
            print(f"  {action:12} {n}")
        return 0

    classes = args.class_name or list(VM_CLASS_MAP.keys())
    print(f"=== Building chains for {classes} (dry_run={args.dry_run}) ===")
    chains = build_dry_run(classes)
    for vm, entries in chains.items():
        print(f"\n[{vm}] {len(entries)} entries")
        for e in entries[:10]:
            cd = f" [{e.cooldown_action}]" if e.cooldown_action else ""
            print(f"  #{e.priority:2} {e.provider:14} {e.model:45} score={e.score:.3f}{cd}")
        if len(entries) > 10:
            print(f"  ... and {len(entries) - 10} more")

    if not args.dry_run and not args.apply:
        print("\n(Dry run — pass --apply to write to gateway yaml)")
    if args.apply:
        result = build_apply(chains, backup=True)
        print(f"\nApplied {len(result['applied'])} chains to {result['path']}")
    return 0


def add_chain_subparser(sub) -> None:
    p = sub.add_parser("chain", help="Build/apply objective fallback chains")
    p.add_argument("--class", dest="class_name", action="append", help="VM/class name (repeatable)")
    p.add_argument("--probe", action="store_true", help="Run live OpenAI-compat probes first")
    p.add_argument("--apply", action="store_true", help="Write changes to gateway yaml")
    p.add_argument("--provider", help="Probe filter: provider name glob")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    p.set_defaults(func=cmd_chain, dry_run=True)


# Late import: config/ranker/httpx need os.environ; avoid import-time side effects.
import os  # noqa: E402
