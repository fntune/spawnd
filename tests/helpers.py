"""Shared helpers for unit tests."""
import sqlite3
from spawnd.storage.db import insert_plan, update_plan_status

def require_row(row: sqlite3.Row | None) -> sqlite3.Row:
    """Return a DB row or fail the test immediately."""
    assert row is not None
    return row

def insert_test_plan(db: sqlite3.Connection, run_id: str, name: str, spec: str='name: test', *, max_cost_usd: float=25.0, status: str | None=None) -> None:
    """Insert a plan row with optional status override."""
    _ = insert_plan(db, run_id, name, spec, max_cost_usd)
    if status is not None:
        _ = update_plan_status(db, run_id, status)
