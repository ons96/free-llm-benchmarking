"""Canonical free-model registry (SQLite + JSON export) for the free-model radar.

One row per (provider, model_id). Holds every field the radar needs to rank
models and decide whether a gateway alias may move: provider, model id,
context, modalities, tool-call support, free quota, rate limit, reset window,
last verified, health, p50 latency, throughput, quality score, source URL and
ToS notes. Probe history, alias state and alias history live alongside it so
the dashboard can show movers and a 7-day stability report.

Free tier only: nothing in this module writes credentials and no probe is
allowed to target a provider whose registry row is not marked free tier.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "free_models.db"
JSON_PATH = REPO_ROOT / "data" / "free_models.json"

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_UNKNOWN = "unknown"
HEALTH_ORDER = {
    HEALTH_HEALTHY: 0,
    HEALTH_DEGRADED: 1,
    HEALTH_UNKNOWN: 2,
    HEALTH_UNHEALTHY: 3,
}

TASK_CLASSES = ("coding", "reasoning", "long-context", "vision", "bulk")

ALIASES = {
    "free-best-coding": "coding",
    "free-best-reasoning": "reasoning",
    "free-best-long": "long-context",
    "free-best-vision": "vision",
    "free-best-bulk": "bulk",
    "free-best-fast": "fast",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    context INTEGER,
    modalities TEXT,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    free_quota TEXT,
    rate_limit TEXT,
    reset_window TEXT,
    last_verified TEXT,
    health TEXT NOT NULL DEFAULT 'unknown',
    p50_latency REAL,
    throughput REAL,
    quality_score REAL,
    source_url TEXT,
    tos_notes TEXT,
    first_seen TEXT,
    last_seen TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    consecutive_healthy INTEGER NOT NULL DEFAULT 0,
    total_probes INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    free_tier INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (provider, model_id)
);

CREATE INDEX IF NOT EXISTS idx_models_health ON models(health);
CREATE INDEX IF NOT EXISTS idx_models_status ON models(status);

CREATE TABLE IF NOT EXISTS probe_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    probe_type TEXT NOT NULL,
    status_code INTEGER,
    ok INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    tokens INTEGER,
    tps REAL,
    error TEXT,
    retry_after REAL
);

CREATE INDEX IF NOT EXISTS idx_probe_model ON probe_history(provider, model_id, ts);

CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    provider TEXT,
    model_id TEXT,
    score REAL,
    updated_at TEXT,
    healthy_streak INTEGER NOT NULL DEFAULT 0,
    fallback_provider TEXT,
    fallback_model_id TEXT
);

CREATE TABLE IF NOT EXISTS alias_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,
    provider TEXT,
    model_id TEXT,
    previous_provider TEXT,
    previous_model_id TEXT,
    action TEXT NOT NULL,
    score REAL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alias_history_alias ON alias_history(alias, ts);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    health TEXT NOT NULL DEFAULT 'unknown',
    last_ok TEXT,
    last_error TEXT,
    cooldown_until TEXT,
    rate_limit_reset_at TEXT,
    free_quota TEXT,
    rate_limit TEXT,
    reset_window TEXT,
    tos_notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    action TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_discovery_ts ON discovery_log(ts);
"""

MODEL_FIELDS = (
    "provider",
    "model_id",
    "context",
    "modalities",
    "tool_calls",
    "free_quota",
    "rate_limit",
    "reset_window",
    "last_verified",
    "health",
    "p50_latency",
    "throughput",
    "quality_score",
    "source_url",
    "tos_notes",
    "first_seen",
    "last_seen",
    "status",
    "consecutive_healthy",
    "total_probes",
    "success_count",
    "free_tier",
)

# Fields a discovery source is allowed to refresh. Never includes health or
# verification derived fields: those are owned by the verifier.
DISCOVERY_FIELDS = (
    "context",
    "modalities",
    "tool_calls",
    "free_quota",
    "rate_limit",
    "reset_window",
    "quality_score",
    "source_url",
    "tos_notes",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    modalities = data.get("modalities")
    if modalities:
        try:
            data["modalities"] = json.loads(modalities)
        except (TypeError, ValueError):
            data["modalities"] = []
    else:
        data["modalities"] = []
    data["tool_calls"] = bool(data.get("tool_calls"))
    data["free_tier"] = bool(data.get("free_tier"))
    return data


def get_model(conn: sqlite3.Connection, provider: str, model_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM models WHERE provider = ? AND model_id = ?",
        (provider, model_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_models(
    conn: sqlite3.Connection,
    active_only: bool = True,
    free_only: bool = True,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if active_only:
        clauses.append("status = 'active'")
    if free_only:
        clauses.append("free_tier = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM models {where}").fetchall()
    return [_row_to_dict(r) for r in rows]


def upsert_model(
    conn: sqlite3.Connection,
    provider: str,
    model_id: str,
    fields: dict[str, Any] | None = None,
    seen_at: str | None = None,
) -> str:
    """Insert or refresh a model row. Returns 'added', 'updated' or 'unchanged'."""
    fields = dict(fields or {})
    seen_at = seen_at or now_iso()
    existing = get_model(conn, provider, model_id)
    allowed = {k: v for k, v in fields.items() if k in DISCOVERY_FIELDS}
    if "modalities" in allowed and isinstance(allowed["modalities"], (list, tuple)):
        allowed["modalities"] = json.dumps(sorted({str(m) for m in allowed["modalities"]}))

    if existing is None:
        cols = ["provider", "model_id", "first_seen", "last_seen", "status", "health"]
        vals: list[Any] = [
            provider,
            model_id,
            seen_at,
            seen_at,
            "active",
            HEALTH_UNKNOWN,
        ]
        for key in MODEL_FIELDS:
            if key in cols:
                continue
            if key in allowed and allowed[key] is not None:
                cols.append(key)
                vals.append(allowed[key])
        conn.execute(
            f"INSERT INTO models ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return "added"

    changed = False
    updates: list[str] = []
    params2: list[Any] = []
    for key, value in allowed.items():
        if value is None:
            continue
        old = existing.get(key)
        if key == "modalities" and old is not None:
            old = json.dumps(old)
        if old != value:
            updates.append(f"{key} = ?")
            params2.append(value)
            changed = True
    updates.append("last_seen = ?")
    params2.append(seen_at)
    updates.append("status = 'active'")
    if changed:
        params2.extend([provider, model_id])
        conn.execute(
            f"UPDATE models SET {','.join(updates)} WHERE provider = ? AND model_id = ?",
            params2,
        )
        return "updated"
    conn.execute(
        "UPDATE models SET last_seen = ?, status = 'active' WHERE provider = ? AND model_id = ?",
        (seen_at, provider, model_id),
    )
    return "unchanged"


def mark_missing(conn: sqlite3.Connection, seen_provider_ids: Iterable[tuple[str, str]]) -> int:
    """Flag active models that a discovery run did not see as removed."""
    seen = set(seen_provider_ids)
    rows = conn.execute("SELECT provider, model_id FROM models WHERE status = 'active'").fetchall()
    missing = [(r["provider"], r["model_id"]) for r in rows if (r["provider"], r["model_id"]) not in seen]
    for provider, model_id in missing:
        conn.execute(
            "UPDATE models SET status = 'removed' WHERE provider = ? AND model_id = ?",
            (provider, model_id),
        )
    return len(missing)


def record_probe(
    conn: sqlite3.Connection,
    provider: str,
    model_id: str,
    probe_type: str,
    ok: bool,
    status_code: int | None = None,
    latency_ms: float | None = None,
    tokens: int | None = None,
    tps: float | None = None,
    error: str | None = None,
    retry_after: float | None = None,
    ts: str | None = None,
) -> None:
    ts = ts or now_iso()
    conn.execute(
        """INSERT INTO probe_history
           (provider, model_id, ts, probe_type, status_code, ok, latency_ms, tokens, tps, error, retry_after)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            provider,
            model_id,
            ts,
            probe_type,
            status_code,
            1 if ok else 0,
            latency_ms,
            tokens,
            tps,
            error,
            retry_after,
        ),
    )


def apply_probe_outcome(
    conn: sqlite3.Connection,
    provider: str,
    model_id: str,
    ok: bool,
    latency_ms: float | None = None,
    tokens: int | None = None,
    tps: float | None = None,
    status_code: int | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Fold a probe result into the model row: health, streak, rolling stats."""
    ts = ts or now_iso()
    model = get_model(conn, provider, model_id)
    if model is None:
        upsert_model(conn, provider, model_id)
        model = get_model(conn, provider, model_id) or {}

    streak = int(model.get("consecutive_healthy") or 0)
    streak = streak + 1 if ok else 0
    total = int(model.get("total_probes") or 0) + 1
    success = int(model.get("success_count") or 0) + (1 if ok else 0)

    p50 = model.get("p50_latency")
    if ok and latency_ms is not None:
        samples = [
            r["latency_ms"]
            for r in conn.execute(
                """SELECT latency_ms FROM probe_history
                   WHERE provider = ? AND model_id = ? AND ok = 1 AND latency_ms IS NOT NULL
                   ORDER BY ts DESC LIMIT 20""",
                (provider, model_id),
            ).fetchall()
        ]
        p50 = percentile(samples, 50)

    throughput = model.get("throughput")
    if ok and tps:
        throughput = round((float(throughput or 0) * 0.7) + (float(tps) * 0.3), 3)

    if ok:
        health = HEALTH_HEALTHY
    elif status_code in (401, 402):
        health = HEALTH_UNHEALTHY
    elif status_code == 429:
        health = HEALTH_DEGRADED
    elif status_code and status_code >= 500:
        health = HEALTH_DEGRADED
    else:
        health = HEALTH_UNHEALTHY

    conn.execute(
        """UPDATE models SET health = ?, consecutive_healthy = ?, total_probes = ?,
               success_count = ?, p50_latency = ?, throughput = ?, last_verified = ?
           WHERE provider = ? AND model_id = ?""",
        (
            health,
            streak,
            total,
            success,
            p50,
            throughput,
            ts,
            provider,
            model_id,
        ),
    )
    return {
        "provider": provider,
        "model_id": model_id,
        "health": health,
        "consecutive_healthy": streak,
        "total_probes": total,
        "success_count": success,
        "p50_latency": p50,
        "throughput": throughput,
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (pct / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


def set_provider_health(
    conn: sqlite3.Connection,
    provider: str,
    health: str,
    last_ok: str | None = None,
    last_error: str | None = None,
    cooldown_until: str | None = None,
    rate_limit_reset_at: str | None = None,
    free_quota: str | None = None,
    rate_limit: str | None = None,
    reset_window: str | None = None,
    tos_notes: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT * FROM provider_health WHERE provider = ?", (provider,)
    ).fetchone()
    existing = dict(row) if row else {}
    conn.execute(
        """INSERT INTO provider_health
           (provider, health, last_ok, last_error, cooldown_until, rate_limit_reset_at,
            free_quota, rate_limit, reset_window, tos_notes, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(provider) DO UPDATE SET
             health = excluded.health,
             last_ok = COALESCE(excluded.last_ok, provider_health.last_ok),
             last_error = excluded.last_error,
             cooldown_until = COALESCE(excluded.cooldown_until, provider_health.cooldown_until),
             rate_limit_reset_at = COALESCE(excluded.rate_limit_reset_at, provider_health.rate_limit_reset_at),
             free_quota = COALESCE(excluded.free_quota, provider_health.free_quota),
             rate_limit = COALESCE(excluded.rate_limit, provider_health.rate_limit),
             reset_window = COALESCE(excluded.reset_window, provider_health.reset_window),
             tos_notes = COALESCE(excluded.tos_notes, provider_health.tos_notes),
             updated_at = excluded.updated_at""",
        (
            provider,
            health,
            last_ok or existing.get("last_ok"),
            last_error,
            cooldown_until,
            rate_limit_reset_at,
            free_quota,
            rate_limit,
            reset_window,
            tos_notes,
            now_iso(),
        ),
    )


def get_provider_health(conn: sqlite3.Connection, provider: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM provider_health WHERE provider = ?", (provider,)
    ).fetchone()
    return dict(row) if row else None


def in_cooldown(conn: sqlite3.Connection, provider: str, now: str | None = None) -> bool:
    """True when the provider is in a rate-limit/backoff window."""
    row = get_provider_health(conn, provider)
    if not row:
        return False
    now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    for key in ("cooldown_until", "rate_limit_reset_at"):
        value = row.get(key)
        if not value:
            continue
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now_dt < until:
            return True
    return False


def get_alias(conn: sqlite3.Connection, alias: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM aliases WHERE alias = ?", (alias,)).fetchone()
    return dict(row) if row else None


def list_aliases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM aliases ORDER BY alias").fetchall()
    return [dict(r) for r in rows]


def set_alias(
    conn: sqlite3.Connection,
    alias: str,
    task: str,
    provider: str | None,
    model_id: str | None,
    score: float | None,
    healthy_streak: int = 0,
    fallback_provider: str | None = None,
    fallback_model_id: str | None = None,
    action: str = "promote",
) -> None:
    row = get_alias(conn, alias)
    prev_provider = row.get("provider") if row else None
    prev_model = row.get("model_id") if row else None
    if row and prev_provider == provider and prev_model == model_id:
        conn.execute(
            "UPDATE aliases SET score = ?, healthy_streak = ?, updated_at = ? WHERE alias = ?",
            (score, healthy_streak, now_iso(), alias),
        )
        return
    conn.execute(
        """INSERT INTO aliases
           (alias, task, provider, model_id, score, updated_at, healthy_streak,
            fallback_provider, fallback_model_id)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(alias) DO UPDATE SET
             task = excluded.task,
             provider = excluded.provider,
             model_id = excluded.model_id,
             score = excluded.score,
             updated_at = excluded.updated_at,
             healthy_streak = excluded.healthy_streak,
             fallback_provider = excluded.fallback_provider,
             fallback_model_id = excluded.fallback_model_id""",
        (
            alias,
            task,
            provider,
            model_id,
            score,
            now_iso(),
            healthy_streak,
            fallback_provider,
            fallback_model_id,
        ),
    )
    conn.execute(
        """INSERT INTO alias_history
           (alias, provider, model_id, previous_provider, previous_model_id, action, score, ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (alias, provider, model_id, prev_provider, prev_model, action, score, now_iso()),
    )


def alias_history(conn: sqlite3.Connection, alias: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if alias:
        rows = conn.execute(
            "SELECT * FROM alias_history WHERE alias = ? ORDER BY ts DESC LIMIT ?",
            (alias, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alias_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def log_discovery(
    conn: sqlite3.Connection,
    source: str,
    provider: str,
    model_id: str,
    action: str,
    ts: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO discovery_log (ts, source, provider, model_id, action) VALUES (?,?,?,?,?)",
        (ts or now_iso(), source, provider, model_id, action),
    )


def stale_entries(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM models
           WHERE status = 'active' AND (last_verified IS NULL OR last_verified < ?)
           ORDER BY last_verified IS NULL DESC, last_verified ASC""",
        (cutoff,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def top_movers(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Models whose health changed inside the window (rank movement proxy)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT provider, model_id,
                  COUNT(*) AS probes,
                  SUM(ok) AS successes,
                  MIN(ts) AS first_ts,
                  MAX(ts) AS last_ts
           FROM probe_history WHERE ts >= ?
           GROUP BY provider, model_id""",
        (since,),
    ).fetchall()
    movers = []
    for r in rows:
        probes = r["probes"] or 0
        successes = r["successes"] or 0
        movers.append(
            {
                "provider": r["provider"],
                "model_id": r["model_id"],
                "probes": probes,
                "successes": successes,
                "success_rate": round(successes / probes, 4) if probes else 0.0,
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
            }
        )
    movers.sort(key=lambda m: (m["success_rate"], m["probes"]), reverse=True)
    return movers


def stability_report(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Per-alias and per-model stability over the trailing window."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    alias_rows = conn.execute(
        """SELECT alias, provider, model_id, updated_at, score FROM aliases ORDER BY alias"""
    ).fetchall()
    aliases = []
    for row in alias_rows:
        stats = conn.execute(
            """SELECT COUNT(*) AS probes, SUM(ok) AS successes, MAX(ts) AS last_probe
               FROM probe_history
               WHERE provider = ? AND model_id = ? AND ts >= ?""",
            (row["provider"], row["model_id"], since),
        ).fetchone()
        probes = stats["probes"] or 0
        successes = stats["successes"] or 0
        changes = conn.execute(
            "SELECT COUNT(*) AS n FROM alias_history WHERE alias = ? AND ts >= ?",
            (row["alias"], since),
        ).fetchone()["n"]
        aliases.append(
            {
                "alias": row["alias"],
                "provider": row["provider"],
                "model_id": row["model_id"],
                "score": row["score"],
                "updated_at": row["updated_at"],
                "last_probe": stats["last_probe"],
                "probes": probes,
                "success_rate": round(successes / probes, 4) if probes else None,
                "alias_changes": changes,
                "stable": bool(probes and successes == probes and changes <= 1),
            }
        )
    return {
        "window_days": days,
        "since": since,
        "generated_at": now_iso(),
        "aliases": aliases,
    }


def export_json(
    conn: sqlite3.Connection,
    json_path: Path | str = JSON_PATH,
    db_path: Path | str = DB_PATH,
) -> Path:
    """Write the canonical JSON mirror of the registry."""
    path = Path(json_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    models = list_models(conn, active_only=False, free_only=False)
    payload = {
        "version": 1,
        "generated_at": now_iso(),
        "database": str(db_path),
        "models": models,
        "aliases": list_aliases(conn),
        "provider_health": [
            dict(r) for r in conn.execute("SELECT * FROM provider_health ORDER BY provider").fetchall()
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path
