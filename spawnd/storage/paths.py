"""Path helpers for spawnd.dev."""

from pathlib import Path


def _new_run_root(run_id: str, base: Path) -> Path:
    return base / ".spawnd" / "runs" / run_id


def _legacy_run_root(run_id: str, base: Path) -> Path:
    return base / ".swarm" / "runs" / run_id


def _run_dir_has_db(run_dir: Path) -> bool:
    return (run_dir / "spawnd.db").exists() or (run_dir / "swarm.db").exists()


def resolve_run_dir(run_id: str, base_path: Path | None = None) -> Path:
    """Run directory for state (DB, logs, worktrees), preferring new layout."""
    base = base_path or Path.cwd()
    new_root = _new_run_root(run_id, base)
    legacy_root = _legacy_run_root(run_id, base)
    if new_root.exists() and _run_dir_has_db(new_root):
        return new_root
    if legacy_root.exists() and _run_dir_has_db(legacy_root):
        return legacy_root
    if new_root.exists():
        return new_root
    if legacy_root.exists():
        return legacy_root
    return new_root


def get_run_dir(run_id: str, base_path: Path | None = None) -> Path:
    """Get the run directory for a run."""
    return resolve_run_dir(run_id, base_path)


def get_db_path(run_id: str, base_path: Path | None = None) -> Path:
    """SQLite path for a run (new installs use ``spawnd.db``)."""
    run_dir = resolve_run_dir(run_id, base_path)
    for name in ("spawnd.db", "swarm.db"):
        candidate = run_dir / name
        if candidate.exists():
            return candidate
    return run_dir / "spawnd.db"


def get_logs_dir(run_id: str, base_path: Path | None = None) -> Path:
    """Get the logs directory for a run."""
    return get_run_dir(run_id, base_path) / "logs"


def get_worktrees_dir(run_id: str, base_path: Path | None = None) -> Path:
    """Get the worktrees directory for a run."""
    return get_run_dir(run_id, base_path) / "worktrees"


def get_log_path(run_id: str, agent_name: str, base_path: Path | None = None) -> Path:
    """Get the log file path for an agent."""
    return get_logs_dir(run_id, base_path) / f"{agent_name}.log"


def ensure_log_file(run_id: str, agent_name: str, base_path: Path | None = None) -> Path:
    """Get or create log file path for an agent."""
    log_path = get_log_path(run_id, agent_name, base_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path
