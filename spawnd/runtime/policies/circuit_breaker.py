"""Circuit breaker policy for scheduler."""
import logging
import sqlite3
from typing import Callable
from spawnd.storage.db import insert_event, update_plan_status
logger = logging.getLogger('spawnd.scheduler.circuit_breaker_policy')

def apply_circuit_breaker_policy(db: sqlite3.Connection, run_id: str, failure_count: int, threshold: int, action: str, cancel_all: Callable[..., None]) -> bool:
    """Apply circuit-breaker action. Returns True when scheduler should stop."""
    if failure_count < threshold:
        return False
    _ = logger.warning(f'Circuit breaker tripped: {failure_count} failures >= {threshold}')
    _ = insert_event(db, run_id, '_system', 'circuit_breaker_tripped', {'failure_count': failure_count, 'threshold': threshold, 'action': action})
    if action == 'cancel_all':
        _ = update_plan_status(db, run_id, 'failed')
        _ = cancel_all('cancelled', 'Circuit breaker tripped', include_pending=True)
        return True
    if action == 'pause':
        _ = update_plan_status(db, run_id, 'paused')
        _ = cancel_all('paused')
        return True
    _ = logger.warning(f'Circuit breaker notification: {failure_count} failures')
    return False
