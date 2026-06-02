"""Agent state machine for legal status transitions."""
import sqlite3
from spawnd.storage.db import update_agent_status
TERMINAL_STATUSES = {'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'}
ALLOWED_TRANSITIONS: dict[str, set[str]] = {'pending': {'running', 'cancelled', 'failed', 'paused'}, 'running': {'blocked', 'checking', 'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded', 'paused'}, 'blocked': {'running', 'timeout', 'cancelled', 'failed'}, 'checking': {'completed', 'failed', 'running', 'cancelled', 'timeout'}, 'paused': {'pending', 'cancelled', 'running'}, 'completed': set(), 'failed': {'pending'}, 'timeout': {'pending'}, 'cancelled': set(), 'cost_exceeded': set()}

def can_transition(current: str | None, target: str) -> bool:
    """Return whether a status transition is legal."""
    if current is None:
        return True
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())

def transition_agent_status(db: sqlite3.Connection, run_id: str, agent_name: str, target_status: str, error: str | None=None, *, current_status: str | None=None, force: bool=False) -> bool:
    """Transition an agent status if legal, optionally forcing the transition."""
    if not force and (not can_transition(current_status, target_status)):
        return False
    _ = update_agent_status(db, run_id, agent_name, target_status, error)
    return True
