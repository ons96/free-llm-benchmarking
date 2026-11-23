"""Offline tests for the nvidia_speedtest_v2 SQLite schema migration
(guarded ALTER TABLE for the token_source column)."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.measurement import BenchmarkRecord  # noqa: E402
import nvidia_speedtest_v2 as v2  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    ttft_ms REAL,
    tps REAL,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    total_time_ms REAL,
    status TEXT NOT NULL,
    error_message TEXT,
    UNIQUE(model)
)
"""


def make_old_db(path):
    """Fixture DB with the pre-token_source 9-column schema plus one row."""
    conn = sqlite3.connect(path)
    conn.execute(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO results (model, timestamp, ttft_ms, tps, status) "
        "VALUES ('meta/llama-old', '2026-01-01T00:00:00', 100.0, 50.0, 'success')"
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_old_schema_and_preserves_data(tmp_path, monkeypatch):
    db = tmp_path / "nvidia_speedtest.db"
    make_old_db(db)
    monkeypatch.setattr(v2, "DB_PATH", db)

    conn = v2.init_db()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        assert "token_source" in cols
        # Pre-existing row survives the migration.
        rows = conn.execute(
            "SELECT model, status FROM results").fetchall()
        assert rows == [("meta/llama-old", "success")]
        # save_result (INSERT naming token_source) no longer crashes.
        v2.save_result(conn, BenchmarkRecord(
            provider="nvidia", model="meta/llama-new",
            status="success", ttft_sec=0.2, tps=80.0,
            output_tokens=100, token_source="usage",
        ))
        got = conn.execute(
            "SELECT token_source FROM results WHERE model = 'meta/llama-new'"
        ).fetchone()
        assert got == ("usage",)
    finally:
        conn.close()


def test_init_db_is_idempotent_on_current_schema(tmp_path, monkeypatch):
    db = tmp_path / "nvidia_speedtest.db"
    monkeypatch.setattr(v2, "DB_PATH", db)

    conn = v2.init_db()  # creates current schema
    conn.close()
    conn = v2.init_db()  # second run must not re-ALTER or crash
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(results)")]
        assert cols.count("token_source") == 1
    finally:
        conn.close()
