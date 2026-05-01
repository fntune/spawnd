"""Log viewing for spawnd.dev."""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from spawnd.storage.paths import ensure_log_file, get_log_path, get_logs_dir

logger = logging.getLogger("spawnd.logs")


def read_log(run_id: str, agent_name: str, lines: int | None = None, base_path: Path | None = None) -> str:
    """Read agent log file.

    Args:
        run_id: Run identifier
        agent_name: Agent name
        lines: Number of lines to read (None for all)
        base_path: Base path for .spawnd directory (defaults to cwd)

    Returns:
        Log content
    """
    log_path = get_log_path(run_id, agent_name, base_path)

    if not log_path.exists():
        return f"Log file not found: {log_path}"

    content = log_path.read_text()

    if lines is not None:
        content_lines = content.split("\n")
        content = "\n".join(content_lines[-lines:])

    return content


def tail_log(
    run_id: str,
    agent_name: str,
    follow: bool = True,
    interval: float = 0.5,
    base_path: Path | None = None,
) -> None:
    """Tail an agent log file.

    Args:
        run_id: Run identifier
        agent_name: Agent name
        follow: If True, continue following
        interval: Poll interval in seconds
        base_path: Base path for .spawnd directory (defaults to cwd)
    """
    log_path = get_log_path(run_id, agent_name, base_path)

    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        print("Waiting for log file to be created...")

        while follow and not log_path.exists():
            time.sleep(interval)

        if not log_path.exists():
            return

    # Read initial content
    with open(log_path) as f:
        content = f.read()
        sys.stdout.write(content)
        sys.stdout.flush()

        if not follow:
            return

        # Follow mode
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(interval)
        except KeyboardInterrupt:
            print("\n")


def list_logs(run_id: str, base_path: Path | None = None) -> list[str]:
    """List available log files for a run.

    Args:
        run_id: Run identifier
        base_path: Base path for .spawnd directory (defaults to cwd)

    Returns:
        List of agent names with logs
    """
    log_dir = get_logs_dir(run_id, base_path)

    if not log_dir.exists():
        return []

    return [p.stem for p in log_dir.glob("*.log")]


def read_all_logs(run_id: str, interleaved: bool = False, base_path: Path | None = None) -> str:
    """Read all logs for a run.

    Args:
        run_id: Run identifier
        interleaved: If True, interleave logs by timestamp
        base_path: Base path for .spawnd directory (defaults to cwd)

    Returns:
        Combined log content
    """
    agent_names = list_logs(run_id, base_path)

    if not agent_names:
        return "No logs found"

    if not interleaved:
        # Concatenate logs
        output = []
        for name in sorted(agent_names):
            output.append(f"\n{'='*60}\n{name}\n{'='*60}\n")
            output.append(read_log(run_id, name, base_path=base_path))
        return "".join(output)

    # Interleave by timestamp (simplified - assumes ISO timestamp prefix)
    entries = []
    for name in agent_names:
        content = read_log(run_id, name, base_path=base_path)
        for line in content.split("\n"):
            if line.strip():
                entries.append((name, line))

    # Sort by timestamp in line (if present)
    # This is a simplified implementation
    entries.sort(key=lambda x: x[1])

    return "\n".join(f"[{name}] {line}" for name, line in entries)


def setup_logging(run_id: str, verbose: bool = False, base_path: Path | None = None) -> None:
    """Set up logging for a run.

    Args:
        run_id: Run identifier
        verbose: Enable debug logging
        base_path: Base path for .spawnd directory (defaults to cwd)
    """
    log_dir = get_logs_dir(run_id, base_path)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Root logger
    root = logging.getLogger("spawnd")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # File handler for main log
    main_log = log_dir / "spawnd.log"
    file_handler = logging.FileHandler(main_log)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console_handler)


def log_to_agent_file(run_id: str, agent_name: str, text: str, base_path: Path | None = None) -> None:
    """Append text to agent log file.

    Args:
        run_id: Run identifier
        agent_name: Agent name
        text: Text to append
        base_path: Base path for .spawnd directory (defaults to cwd)
    """
    log_path = ensure_log_file(run_id, agent_name, base_path)

    with open(log_path, "a") as f:
        timestamp = datetime.now().isoformat()
        f.write(f"[{timestamp}] {text}\n")
