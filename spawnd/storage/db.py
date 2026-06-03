"""SQLite database setup and queries for spawnd.dev."""
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from uuid import uuid4
from spawnd.storage.paths import get_db_path
logger = logging.getLogger('spawnd.db')
SCHEMA = "\n-- Plans table\nCREATE TABLE IF NOT EXISTS plans (\n    run_id TEXT PRIMARY KEY,\n    name TEXT NOT NULL,\n    spec TEXT NOT NULL,\n    status TEXT DEFAULT 'running',\n    total_cost_usd REAL DEFAULT 0.0,\n    max_cost_usd REAL DEFAULT 25.0,\n    created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    updated_at TEXT DEFAULT CURRENT_TIMESTAMP\n);\n\n-- Agents table\nCREATE TABLE IF NOT EXISTS agents (\n    run_id TEXT NOT NULL,\n    name TEXT NOT NULL,\n    plan_name TEXT,\n    status TEXT NOT NULL DEFAULT 'pending',\n    type TEXT DEFAULT 'worker',\n    runtime TEXT NOT NULL DEFAULT 'claude',\n    iteration INTEGER DEFAULT 0,\n    max_iterations INTEGER DEFAULT 30,\n    worktree TEXT,\n    branch TEXT,\n    prompt TEXT,\n    check_command TEXT,\n    model TEXT DEFAULT 'sonnet',\n    parent TEXT,\n    vendor_session_id TEXT,\n    pid INTEGER,\n    cost_usd REAL DEFAULT 0.0,\n    cost_source TEXT NOT NULL DEFAULT 'sdk',\n    max_cost_usd REAL DEFAULT 5.0,\n    error TEXT,\n    depends_on TEXT,\n    on_failure TEXT DEFAULT 'continue',\n    retry_count INTEGER DEFAULT 3,\n    retry_attempt INTEGER DEFAULT 0,\n    last_error TEXT,\n    env TEXT,\n    max_subagents INTEGER,\n    created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    PRIMARY KEY (run_id, name),\n    FOREIGN KEY (run_id) REFERENCES plans(run_id)\n);\n\n-- Events table\nCREATE TABLE IF NOT EXISTS events (\n    id TEXT PRIMARY KEY,\n    run_id TEXT NOT NULL,\n    ts TEXT DEFAULT CURRENT_TIMESTAMP,\n    agent TEXT NOT NULL,\n    event_type TEXT NOT NULL,\n    data TEXT,\n    FOREIGN KEY (run_id, agent) REFERENCES agents(run_id, name)\n);\n\n-- Responses table\nCREATE TABLE IF NOT EXISTS responses (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    run_id TEXT NOT NULL,\n    clarification_id TEXT NOT NULL,\n    response TEXT NOT NULL,\n    consumed INTEGER DEFAULT 0,\n    created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    FOREIGN KEY (clarification_id) REFERENCES events(id)\n);\n\n-- Indexes\nCREATE INDEX IF NOT EXISTS idx_agents_run_status ON agents(run_id, status);\nCREATE INDEX IF NOT EXISTS idx_agents_run_parent ON agents(run_id, parent);\nCREATE INDEX IF NOT EXISTS idx_events_run_agent ON events(run_id, agent);\nCREATE INDEX IF NOT EXISTS idx_events_run_type ON events(run_id, event_type);\nCREATE INDEX IF NOT EXISTS idx_responses_pending ON responses(run_id, clarification_id, consumed);\n"

def open_db(run_id: str, base_path: Path | None=None) -> sqlite3.Connection:
    """Open database with proper concurrency settings."""
    db_path = get_db_path(run_id, base_path)
    _ = db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=30.0)
    db.row_factory = sqlite3.Row
    _ = db.execute('PRAGMA journal_mode = WAL')
    _ = db.execute('PRAGMA busy_timeout = 5000')
    _ = db.execute('PRAGMA synchronous = NORMAL')
    return db

@contextmanager
def get_db(run_id: str, base_path: Path | None=None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database access.

    Usage:
        with get_db(run_id) as db:
            agent = get_agent(db, run_id, name)
    """
    db = open_db(run_id, base_path)
    try:
        yield db
    finally:
        _ = db.close()

def init_db(run_id: str, base_path: Path | None=None) -> sqlite3.Connection:
    """Initialize database with schema, migrating any existing DB forward."""
    db = open_db(run_id, base_path)
    _ = db.executescript(SCHEMA)
    _ = _migrate_agents(db)
    _ = db.commit()
    _ = logger.info(f'Initialized database at {get_db_path(run_id, base_path)}')
    return db

def _migrate_agents(db: sqlite3.Connection) -> None:
    """Idempotent migration for the agents table.

    Adds runtime and cost_source columns and renames session_id to
    vendor_session_id on DBs created by older releases. Safe to run on a
    fresh DB (all checks become no-ops) or a fully-migrated DB.
    """
    columns = {row['name'] for row in db.execute('PRAGMA table_info(agents)')}
    if 'runtime' not in columns:
        _ = db.execute("ALTER TABLE agents ADD COLUMN runtime TEXT NOT NULL DEFAULT 'claude'")
    if 'cost_source' not in columns:
        _ = db.execute("ALTER TABLE agents ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'sdk'")
    if 'session_id' in columns and 'vendor_session_id' not in columns:
        _ = db.execute('ALTER TABLE agents RENAME COLUMN session_id TO vendor_session_id')

def insert_plan(db: sqlite3.Connection, run_id: str, name: str, spec: str, max_cost_usd: float=25.0, *, autocommit: bool=True) -> None:
    """Insert a plan record."""
    _ = db.execute('INSERT INTO plans (run_id, name, spec, max_cost_usd) VALUES (?, ?, ?, ?)', (run_id, name, spec, max_cost_usd))
    if autocommit:
        _ = db.commit()

def insert_agent(db: sqlite3.Connection, run_id: str, name: str, prompt: str, agent_type: str='worker', check_command: str | None=None, model: str | None='sonnet', max_iterations: int=30, max_cost_usd: float=5.0, depends_on: list[str] | None=None, parent: str | None=None, plan_name: str | None=None, on_failure: str='continue', retry_count: int=3, env: dict[str, str] | None=None, max_subagents: int | None=None, runtime: str='claude', cost_source: str='sdk', *, autocommit: bool=True) -> None:
    """Insert an agent record."""
    _ = db.execute('INSERT INTO agents (\n            run_id, name, plan_name, type, prompt, check_command, model,\n            max_iterations, max_cost_usd, depends_on, parent, on_failure, retry_count,\n            env, max_subagents, runtime, cost_source\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (run_id, name, plan_name, agent_type, prompt, check_command, model, max_iterations, max_cost_usd, json.dumps(depends_on or []), parent, on_failure, retry_count, json.dumps(env) if env else None, max_subagents, runtime, cost_source))
    if autocommit:
        _ = db.commit()

def update_agent_status(db: sqlite3.Connection, run_id: str, name: str, status: str, error: str | None=None, *, autocommit: bool=True) -> None:
    """Update agent status."""
    if error:
        _ = db.execute('UPDATE agents SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ? AND name = ?', (status, error, run_id, name))
    else:
        _ = db.execute('UPDATE agents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ? AND name = ?', (status, run_id, name))
    if autocommit:
        _ = db.commit()

def update_agent_worktree(db: sqlite3.Connection, run_id: str, name: str, worktree: str, branch: str, *, autocommit: bool=True) -> None:
    """Update agent worktree and branch."""
    _ = db.execute('UPDATE agents SET worktree = ?, branch = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ? AND name = ?', (worktree, branch, run_id, name))
    if autocommit:
        _ = db.commit()

def update_agent_iteration(db: sqlite3.Connection, run_id: str, name: str, iteration: int, *, autocommit: bool=True) -> None:
    """Update agent iteration count."""
    _ = db.execute('UPDATE agents SET iteration = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ? AND name = ?', (iteration, run_id, name))
    if autocommit:
        _ = db.commit()

def update_agent_cost(db: sqlite3.Connection, run_id: str, name: str, cost_usd: float, *, autocommit: bool=True) -> None:
    """Update agent cost."""
    _ = db.execute('UPDATE agents SET cost_usd = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ? AND name = ?', (cost_usd, run_id, name))
    if autocommit:
        _ = db.commit()

def get_agent(db: sqlite3.Connection, run_id: str, name: str) -> sqlite3.Row | None:
    """Get a single agent by name."""
    return db.execute('SELECT * FROM agents WHERE run_id = ? AND name = ?', (run_id, name)).fetchone()

def get_agents(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get all agents for a run."""
    return db.execute('SELECT * FROM agents WHERE run_id = ? ORDER BY created_at', (run_id,)).fetchall()

def get_pending_agents(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get agents ready to start (pending with completed deps)."""
    return db.execute("\n        SELECT a.* FROM agents a\n        WHERE a.run_id = ? AND a.status = 'pending'\n        AND NOT EXISTS (\n            SELECT 1 FROM agents dep\n            WHERE dep.run_id = a.run_id\n            AND dep.name IN (SELECT value FROM json_each(a.depends_on))\n            AND dep.status NOT IN ('completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded')\n        )\n        ", (run_id,)).fetchall()

def get_running_agents(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get currently running agents."""
    return db.execute("SELECT * FROM agents WHERE run_id = ? AND status IN ('running', 'blocked', 'checking')", (run_id,)).fetchall()

def all_agents_done(db: sqlite3.Connection, run_id: str) -> bool:
    """Check if all agents are in terminal state."""
    pending = db.execute("\n        SELECT COUNT(*) FROM agents\n        WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'timeout', 'cost_exceeded', 'cancelled', 'paused')\n        ", (run_id,)).fetchone()[0]
    return pending == 0

def insert_event(db: sqlite3.Connection, run_id: str, agent: str, event_type: str, data: dict | None=None, *, autocommit: bool=True) -> str:
    """Insert an event and return its ID."""
    event_id = uuid4().hex
    _ = db.execute('INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, ?, ?)', (event_id, run_id, agent, event_type, json.dumps(data or {})))
    if autocommit:
        _ = db.commit()
    return event_id

def get_recent_events(db: sqlite3.Connection, run_id: str, since_seconds: int=30, limit: int=50) -> list[sqlite3.Row]:
    """Get recent events."""
    return db.execute("\n        SELECT id, agent, event_type, data, ts FROM events\n        WHERE run_id = ? AND ts > datetime('now', ?)\n        ORDER BY ts DESC LIMIT ?\n        ", (run_id, f'-{since_seconds} seconds', limit)).fetchall()

def get_pending_clarifications(db: sqlite3.Connection, run_id: str) -> list[dict]:
    """Get clarifications awaiting response."""
    rows = db.execute("\n        SELECT e.id, e.agent, json_extract(e.data, '$.question') as question\n        FROM events e\n        WHERE e.run_id = ? AND e.event_type IN ('clarification', 'blocker')\n        AND NOT EXISTS (SELECT 1 FROM responses r WHERE r.clarification_id = e.id)\n        ", (run_id,)).fetchall()
    return [dict(r) for r in rows]

def insert_response(db: sqlite3.Connection, run_id: str, clarification_id: str, response: str, *, autocommit: bool=True) -> None:
    """Insert a response to a clarification."""
    _ = db.execute('INSERT INTO responses (run_id, clarification_id, response) VALUES (?, ?, ?)', (run_id, clarification_id, response))
    if autocommit:
        _ = db.commit()

def get_response(db: sqlite3.Connection, run_id: str, clarification_id: str) -> sqlite3.Row | None:
    """Get unconsumed response for a clarification."""
    return db.execute('\n        SELECT id, response FROM responses\n        WHERE run_id = ? AND clarification_id = ? AND consumed = 0\n        LIMIT 1\n        ', (run_id, clarification_id)).fetchone()

def consume_response(db: sqlite3.Connection, response_id: int, *, autocommit: bool=True) -> None:
    """Mark a response as consumed."""
    _ = db.execute('UPDATE responses SET consumed = 1 WHERE id = ?', (response_id,))
    if autocommit:
        _ = db.commit()

def get_total_cost(db: sqlite3.Connection, run_id: str) -> float:
    """Get total cost for a run."""
    result = db.execute('SELECT SUM(cost_usd) FROM agents WHERE run_id = ?', (run_id,)).fetchone()[0]
    return result or 0.0

def update_plan_status(db: sqlite3.Connection, run_id: str, status: str, *, autocommit: bool=True) -> None:
    """Update plan status."""
    _ = db.execute('UPDATE plans SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?', (status, run_id))
    if autocommit:
        _ = db.commit()

def get_plan(db: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Get plan by run_id."""
    return db.execute('SELECT *, max_cost_usd as budget_usd FROM plans WHERE run_id = ?', (run_id,)).fetchone()

def run_exists(run_id: str, base_path: Path | None=None) -> bool:
    """Check if a run exists."""
    db_path = get_db_path(run_id, base_path)
    return db_path.exists()

def increment_retry_attempt(db: sqlite3.Connection, run_id: str, name: str, error: str, *, autocommit: bool=True) -> int:
    """Increment retry attempt and save error. Returns new attempt count."""
    _ = db.execute('UPDATE agents SET\n           retry_attempt = retry_attempt + 1,\n           last_error = ?,\n           updated_at = CURRENT_TIMESTAMP\n           WHERE run_id = ? AND name = ?', (error, run_id, name))
    if autocommit:
        _ = db.commit()
    row = db.execute('SELECT retry_attempt FROM agents WHERE run_id = ? AND name = ?', (run_id, name)).fetchone()
    return row[0] if row else 0

def get_retryable_agents(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Get failed agents that can be retried."""
    return db.execute("SELECT * FROM agents\n           WHERE run_id = ? AND status = 'failed'\n           AND on_failure = 'retry' AND retry_attempt <= retry_count", (run_id,)).fetchall()

def reset_agent_for_retry(db: sqlite3.Connection, run_id: str, name: str) -> None:
    """Reset a failed agent to pending for retry."""
    _ = db.execute("UPDATE agents SET\n           status = 'pending',\n           error = NULL,\n           iteration = 0,\n           updated_at = CURRENT_TIMESTAMP\n           WHERE run_id = ? AND name = ?", (run_id, name))
    _ = db.commit()
    _ = logger.info(f'Reset agent {name} for retry')

def reset_failed_agents(db: sqlite3.Connection, run_id: str) -> list[str]:
    """Reset failed/timeout agents to pending for retry.

    Returns list of agent names that were reset.
    """
    agents = db.execute("SELECT name FROM agents\n           WHERE run_id = ?\n             AND status IN ('failed', 'timeout')\n             AND on_failure = 'retry'\n             AND retry_attempt <= retry_count", (run_id,)).fetchall()
    names = [a['name'] for a in agents]
    if names:
        _ = db.execute("UPDATE agents SET status = 'pending', error = NULL, iteration = 0,\n               updated_at = CURRENT_TIMESTAMP\n               WHERE run_id = ?\n                 AND status IN ('failed', 'timeout')\n                 AND on_failure = 'retry'\n                 AND retry_attempt <= retry_count", (run_id,))
        _ = db.commit()
        _ = logger.info(f'Reset {len(names)} agents for retry: {names}')
    return names

def reset_paused_agents(db: sqlite3.Connection, run_id: str) -> list[str]:
    """Reset paused agents to pending so a paused run can resume."""
    agents = db.execute("SELECT name FROM agents\n           WHERE run_id = ?\n             AND status = 'paused'", (run_id,)).fetchall()
    names = [a['name'] for a in agents]
    if names:
        _ = db.execute("UPDATE agents SET status = 'pending', error = NULL, iteration = 0,\n               updated_at = CURRENT_TIMESTAMP\n               WHERE run_id = ?\n                 AND status = 'paused'", (run_id,))
        _ = db.commit()
        _ = logger.info(f'Reset {len(names)} paused agents for resume: {names}')
    return names

def list_runs(base_path: Path | None=None) -> list[str]:
    """List all run IDs (new ``.spawnd`` layout and legacy ``.swarm``)."""
    base = base_path or Path.cwd()
    candidates: dict[str, float] = {}
    for root_name in ('.spawnd', '.swarm'):
        runs_dir = base / root_name / 'runs'
        if not runs_dir.exists():
            continue
        for d in runs_dir.iterdir():
            if not d.is_dir():
                continue
            mtime = d.stat().st_mtime
            prev = candidates.get(d.name)
            if prev is None or mtime > prev:
                candidates[d.name] = mtime
    if not candidates:
        return []
    names = sorted(candidates.keys(), key=lambda n: (candidates[n], n), reverse=True)
    return names
