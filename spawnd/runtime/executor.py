"""Agent execution dispatch.

The scheduler calls ``spawn_worker`` / ``spawn_manager`` with an
``AgentConfig``. This module builds the appropriate ``Toolset`` and
dispatches to the registered ``Executor`` for ``config.runtime``. The
actual vendor integration lives in ``spawnd.runtime.executors``.
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Side-effect import: registers ClaudeExecutor (and OpenAIExecutor if the
# optional extra is installed) in the executor registry.
from spawnd.runtime import executors as _executors  # pyright: ignore[reportUnusedImport] # noqa: F401
from spawnd.runtime.executors.base import get_executor
from spawnd.storage.db import (
    insert_event,
    open_db,
    update_agent_status,
)
from spawnd.tools.toolset import manager_toolset, worker_toolset

logger = logging.getLogger("spawnd.executor")


@dataclass
class AgentConfig:
    """Configuration for running an agent."""

    name: str
    run_id: str
    prompt: str
    worktree: Path
    check_command: str = "true"
    model: str = "sonnet"
    max_iterations: int = 30
    max_cost_usd: float = 5.0
    parent: str | None = None
    env: dict | None = None
    shared_context: str = ""
    runtime: str = "claude"

    def tree_path(self) -> str:
        """Get full hierarchy path."""
        if self.parent and not self.name.startswith(f"{self.parent}."):
            return f"{self.parent}.{self.name}"
        return self.name


def build_system_prompt(config: AgentConfig) -> str:
    """Build system prompt for worker agent."""
    return f"""You are an autonomous coding agent working on a specific task.

Task: {config.prompt}

Check command: {config.check_command}

When you have completed the task:
1. Run the check command to verify your work
2. If the check passes, your task is complete
3. If the check fails, fix the issues and try again

Important:
- Focus only on the assigned task
- Commit your changes frequently
- If you encounter a blocker, describe it clearly

{config.shared_context}
"""


def build_manager_system_prompt(config: AgentConfig) -> str:
    """Build system prompt for manager agent."""
    return f"""You are a manager agent coordinating worker agents.

Task: {config.prompt}

Your tools:
- spawn_worker: Create new workers to handle subtasks
- get_worker_status: Check worker progress
- get_pending_clarifications: See worker questions
- respond_to_clarification: Answer worker questions
- cancel_worker: Stop a worker
- mark_plan_complete: Signal when done (all workers must be complete first)

Orchestrate the work, respond to clarifications, and call mark_plan_complete when finished.

{config.shared_context}
"""


async def run_worker(config: AgentConfig) -> dict:
    """Run a worker agent on its configured runtime."""
    toolset = worker_toolset(system_prompt=build_system_prompt(config))
    return await get_executor(config.runtime).run(config, toolset)


async def run_manager(config: AgentConfig) -> dict:
    """Run a manager agent on its configured runtime."""
    toolset = manager_toolset(system_prompt=build_manager_system_prompt(config))
    return await get_executor(config.runtime).run(config, toolset)


async def run_worker_mock(config: AgentConfig) -> dict:
    """Mock worker for testing without any vendor SDK."""
    db = open_db(config.run_id)

    try:
        update_agent_status(db, config.run_id, config.name, "running")
        insert_event(db, config.run_id, config.name, "started", {"prompt": config.prompt[:200]})

        await asyncio.sleep(0.5)

        result = subprocess.run(
            config.check_command,
            shell=True,
            cwd=str(config.worktree),
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            update_agent_status(db, config.run_id, config.name, "completed")
            insert_event(db, config.run_id, config.name, "done", {"summary": "Mock task completed"})
            return {"success": True, "status": "completed"}

        update_agent_status(db, config.run_id, config.name, "failed", result.stderr[:500])
        insert_event(db, config.run_id, config.name, "error", {"error": result.stderr[:200]})
        return {"success": False, "status": "failed", "error": result.stderr[:500]}

    except Exception as e:
        logger.error(f"Mock worker {config.name} failed: {e}")
        update_agent_status(db, config.run_id, config.name, "failed", str(e))
        return {"success": False, "status": "failed", "error": str(e)}

    finally:
        db.close()


def spawn_worker(config: AgentConfig, use_mock: bool = False) -> asyncio.Task:
    """Spawn a worker agent as an asyncio task."""
    if use_mock:
        return asyncio.create_task(run_worker_mock(config), name=f"worker-{config.name}")
    return asyncio.create_task(run_worker(config), name=f"worker-{config.name}")


def spawn_manager(config: AgentConfig) -> asyncio.Task:
    """Spawn a manager agent as an asyncio task."""
    return asyncio.create_task(run_manager(config), name=f"manager-{config.name}")
