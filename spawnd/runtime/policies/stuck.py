"""Stuck-run detection policy."""
import logging
import sqlite3
from spawnd.storage.db import insert_event
logger = logging.getLogger('spawnd.scheduler.stuck_policy')

def evaluate_stuck_run(db: sqlite3.Connection, run_id: str, events: list, has_live_tasks: bool, last_event_marker: str | None, idle_iterations: int, threshold: int) -> tuple[bool, str | None, int]:
    """Return (is_stuck, new_marker, new_idle_iterations)."""
    current_marker = events[0]['id'] if events else None
    if current_marker == last_event_marker:
        idle_iterations += 1
    else:
        idle_iterations = 0
    if idle_iterations >= threshold:
        if has_live_tasks:
            _ = logger.info(f'Run is waiting on live tasks: {idle_iterations} iterations with no events')
            marker = insert_event(db, run_id, '_system', 'progress', {'status': 'waiting_for_live_tasks', 'idle_iterations': idle_iterations})
            return (False, marker, 0)
        _ = logger.warning(f'Run appears stuck: {idle_iterations} iterations with no events and no live tasks')
        _ = insert_event(db, run_id, '_system', 'error', {'error': 'stuck_detected', 'idle_iterations': idle_iterations})
        return (True, current_marker, idle_iterations)
    return (False, current_marker, idle_iterations)
