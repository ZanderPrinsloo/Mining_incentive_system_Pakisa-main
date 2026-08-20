"""
scenario_store.py
------------------
Local persistence for named Bonus Policy Simulator scenarios.

Lets the CFO/mine manager save a parameter set ("what if we cut the break
rate 10% and raise the driller bonus") under a name, come back later, and
compare it against other saved proposals and current policy — instead of the
simulation being a one-off that vanishes on refresh.

Same local-SQLite approach as target_store.py, and for the same reason: this
is app UI state, not SQL Server reporting data. Results are frozen at save
time (not recomputed live) so a scenario stays comparable even if someone
later changes the dashboard's period/section filter.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "dashboard_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bonus_scenarios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT,
    period_from  INTEGER,
    period_to    INTEGER,
    section      TEXT,
    params_json  TEXT NOT NULL,
    results_json TEXT NOT NULL
);
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


def _row_to_dict(row: tuple) -> dict:
    (id_, name, created_at, created_by, period_from, period_to, section,
     params_json, results_json) = row
    return {
        "id": id_,
        "name": name,
        "created_at": created_at,
        "created_by": created_by,
        "period_from": period_from,
        "period_to": period_to,
        "section": section,
        "params": json.loads(params_json),
        "results": json.loads(results_json),
    }


def save_scenario(name: str, period_from: int, period_to: int, section: str,
                  params: dict, results: dict, created_by: Optional[str] = None) -> dict:
    if not name:
        raise ValueError("name is required")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO bonus_scenarios
               (name, created_at, created_by, period_from, period_to, section, params_json, results_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, now, created_by, period_from, period_to, section,
             json.dumps(params), json.dumps(results)),
        )
        new_id = cur.lastrowid
    return {
        "id": new_id, "name": name, "created_at": now, "created_by": created_by,
        "period_from": period_from, "period_to": period_to, "section": section,
        "params": params, "results": results,
    }


def list_scenarios() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, name, created_at, created_by, period_from, period_to,
                      section, params_json, results_json
               FROM bonus_scenarios ORDER BY id DESC"""
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_scenario(scenario_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM bonus_scenarios WHERE id = ?", (scenario_id,))
