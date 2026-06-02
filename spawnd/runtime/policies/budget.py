"""Run budget policy for scheduler."""
import logging
import sqlite3
from typing import Callable
from spawnd.storage.db import insert_event, update_plan_status
logger = logging.getLogger('spawnd.scheduler.budget_policy')

def apply_run_budget_policy(db: sqlite3.Connection, run_id: str, total_cost: float, budget: float, action: str, cancel_all: Callable[..., None]) -> bool:
    """Apply cost budget policy. Returns True when scheduler should stop."""
    _ = logger.warning(f'Run {run_id}: cost budget exceeded (${total_cost:.2f} > ${budget:.2f}), action={action}')
    _ = insert_event(db, run_id, '_system', 'error', {'error': 'cost_exceeded', 'total_cost': total_cost, 'budget': budget, 'action': action})
    if action == 'warn':
        return False
    if action == 'cancel':
        _ = update_plan_status(db, run_id, 'failed')
        _ = cancel_all('cancelled', 'Cost budget exceeded')
        return True
    _ = update_plan_status(db, run_id, 'paused')
    _ = cancel_all('paused')
    return True
