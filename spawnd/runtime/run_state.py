"""Run-level existence and resume guards shared across API and CLI."""

import sqlite3

from spawnd.storage.db import get_db, get_plan, run_exists


def run_has_persisted_plan(run_id: str) -> bool:
    """Return True when ``run_id`` points at a readable run with a stored plan."""
    if not run_exists(run_id):
        return False

    try:
        with get_db(run_id) as db:
            has_plans_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plans'"
            ).fetchone()
            return bool(has_plans_table and get_plan(db, run_id))
    except sqlite3.Error:
        return False
