"""
target_store.py
----------------
Lightweight local persistence for dashboard targets (Forecast tab).

Deliberately NOT stored in the SQL Server database that web/queries.py reads
from — that's read-only reporting data pulled from Harmony's mine systems,
not a place for this app's own UI state. Targets live in a small local
SQLite file instead. Writes are append-only, so "what was the target last
month" is a query, not a guess.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "dashboard_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL,
    value      REAL NOT NULL,
    set_by     TEXT,
    set_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_key ON targets(target_key, id);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_current_targets() -> dict:
    """Latest value per target_key.

    Returns {key: {"value": float, "set_at": iso-str, "set_by": str|None}}.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT t.target_key, t.value, t.set_at, t.set_by
            FROM targets t
            INNER JOIN (
                SELECT target_key, MAX(id) AS max_id FROM targets GROUP BY target_key
            ) latest ON t.target_key = latest.target_key AND t.id = latest.max_id
        """).fetchall()
    return {r[0]: {"value": r[1], "set_at": r[2], "set_by": r[3]} for r in rows}


def set_target(target_key: str, value: float, set_by: Optional[str] = None) -> dict:
    if not target_key:
        raise ValueError("target_key is required")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO targets (target_key, value, set_by, set_at) VALUES (?, ?, ?, ?)",
            (target_key, float(value), set_by, now),
        )
    return {"target_key": target_key, "value": float(value), "set_at": now, "set_by": set_by}


def get_target_history(target_key: str, limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT value, set_at, set_by FROM targets WHERE target_key = ? "
            "ORDER BY id DESC LIMIT ?",
            (target_key, limit),
        ).fetchall()
    return [{"value": r[0], "set_at": r[1], "set_by": r[2]} for r in rows]
