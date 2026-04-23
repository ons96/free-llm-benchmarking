"""SQLite schema and helpers for llm-speedrun."""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

DB_PATH = Path(__file__).parent / "data" / "speedrun.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS speed_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    model_name TEXT NOT NULL,
    source TEXT NOT NULL,
    reasoning_effort TEXT,
    ttft_ms REAL,
    tps REAL,
    output_tokens INTEGER,
    total_time_ms REAL,
    status TEXT NOT NULL,
    error_message TEXT,
    run_number INTEGER NOT NULL,
    raw_sample TEXT
);

CREATE INDEX IF NOT EXISTS idx_speed_tests_run_id ON speed_tests(run_id);
CREATE INDEX IF NOT EXISTS idx_speed_tests_model ON speed_tests(provider_name, model_name);

CREATE TABLE IF NOT EXISTS speed_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    model_name TEXT NOT NULL,
    source TEXT NOT NULL,
    reasoning_effort TEXT,
    avg_ttft_ms REAL,
    avg_tps REAL,
    avg_total_time_ms REAL,
    est_10k_total_s REAL,
    num_runs INTEGER,
    num_success INTEGER
);

CREATE INDEX IF NOT EXISTS idx_summary_run ON speed_summary(run_id);
CREATE INDEX IF NOT EXISTS idx_summary_model ON speed_summary(provider_name, model_name);

CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_canonical TEXT NOT NULL,
    benchmark_source TEXT NOT NULL,
    reasoning_effort TEXT,
    score REAL,
    score_label TEXT,
    fetched_at TEXT NOT NULL,
    raw_data TEXT,
    UNIQUE(model_canonical, benchmark_source, reasoning_effort)
);

CREATE INDEX IF NOT EXISTS idx_bench_canonical ON benchmarks(model_canonical);

CREATE TABLE IF NOT EXISTS model_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    alias TEXT NOT NULL UNIQUE
);

CREATE VIEW IF NOT EXISTS v_latest_leaderboard AS
SELECT s.*
FROM speed_summary s
INNER JOIN (
    SELECT provider_name, model_name, reasoning_effort, MAX(timestamp) AS max_ts
    FROM speed_summary
    GROUP BY provider_name, model_name, reasoning_effort
) latest
ON s.provider_name = latest.provider_name
 AND s.model_name = latest.model_name
 AND (s.reasoning_effort IS latest.reasoning_effort OR s.reasoning_effort = latest.reasoning_effort)
 AND s.timestamp = latest.max_ts
ORDER BY s.est_10k_total_s ASC;
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


TEST_COLS = frozenset(
    {
        "id",
        "run_id",
        "timestamp",
        "provider_name",
        "provider_url",
        "model_name",
        "source",
        "reasoning_effort",
        "ttft_ms",
        "tps",
        "output_tokens",
        "total_time_ms",
        "status",
        "error_message",
        "run_number",
        "raw_sample",
    }
)
SUMMARY_COLS = frozenset(
    {
        "id",
        "run_id",
        "timestamp",
        "provider_name",
        "provider_url",
        "model_name",
        "source",
        "reasoning_effort",
        "avg_ttft_ms",
        "avg_tps",
        "avg_total_time_ms",
        "est_10k_total_s",
        "num_runs",
        "num_success",
    }
)


def _sanitize_cols(kwargs: dict, allowed: frozenset) -> tuple:
    cols, vals = [], []
    for k, v in kwargs.items():
        if k in allowed:
            cols.append(k)
            vals.append(v)
    return cols, vals


def insert_test(conn: sqlite3.Connection, **kwargs: Any) -> None:
    cols, vals = _sanitize_cols(kwargs, TEST_COLS)
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO speed_tests ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def insert_summary(conn: sqlite3.Connection, **kwargs: Any) -> None:
    cols, vals = _sanitize_cols(kwargs, SUMMARY_COLS)
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO speed_summary ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def upsert_benchmark(
    conn: sqlite3.Connection,
    model_canonical: str,
    benchmark_source: str,
    reasoning_effort: str | None,
    score: float | None,
    score_label: str,
    raw_data: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO benchmarks (model_canonical, benchmark_source, reasoning_effort,
                                score, score_label, fetched_at, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_canonical, benchmark_source, reasoning_effort)
        DO UPDATE SET score=excluded.score, score_label=excluded.score_label,
                      fetched_at=excluded.fetched_at, raw_data=excluded.raw_data
        """,
        (
            model_canonical,
            benchmark_source,
            reasoning_effort,
            score,
            score_label,
            now_iso(),
            json.dumps(raw_data),
        ),
    )


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
