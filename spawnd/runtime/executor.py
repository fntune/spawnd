"""Agent execution dispatch.

The scheduler calls ``spawn_worker`` / ``spawn_manager`` with an
``AgentConfig``. This module builds the appropriate ``Toolset`` and
dispatches to the registered ``Executor`` for ``config.runtime``. The
actual vendor integration lives in ``spawnd.runtime.executors``.
"""
import asyncio
import logging
import subprocess
from spawnd.runtime.agent_run import AgentConfig
from spawnd.storage.db import insert_event, open_db, update_agent_status
from spawnd.tools.toolset import manager_toolset, worker_toolset
logger = logging.getLogger('spawnd.executor')
_executors_loaded = False

def _load_executors() -> None:
    """Register bundled executors on first dispatch."""
    global _executors_loaded
    if _executors_loaded:
        return
    from spawnd.runtime import executors as _executors  # pyright: ignore[reportUnusedImport] # noqa: F401

    _executors_loaded = True


def _get_executor(runtime: str):
    _load_executors()
    from spawnd.runtime.executors.base import get_executor
    return get_executor(runtime)

def build_system_prompt(config: AgentConfig) -> str:
    """Build system prompt for worker agent."""
    return f'You are an autonomous coding agent working on a specific task.\n\nTask: {config.prompt}\n\nCheck command: {config.check_command}\n\nWhen you have completed the task:\n1. Run the check command to verify your work\n2. If the check passes, your task is complete\n3. If the check fails, fix the issues and try again\n\nImportant:\n- Focus only on the assigned task\n- Commit your changes frequently\n- If you encounter a blocker, describe it clearly\n\n{config.shared_context}\n'

def build_manager_system_prompt(config: AgentConfig) -> str:
    """Build system prompt for manager agent."""
    return f'You are a manager agent coordinating worker agents.\n\nTask: {config.prompt}\n\nYour tools:\n- spawn_worker: Create new workers to handle subtasks\n- get_worker_status: Check worker progress\n- get_pending_clarifications: See worker questions\n- respond_to_clarification: Answer worker questions\n- cancel_worker: Stop a worker\n- mark_plan_complete: Signal when done (all workers must be complete first)\n\nOrchestrate the work, respond to clarifications, and call mark_plan_complete when finished.\n\n{config.shared_context}\n'

async def run_worker(config: AgentConfig) -> dict:
    """Run a worker agent on its configured runtime."""
    toolset = worker_toolset(system_prompt=build_system_prompt(config))
    return await _get_executor(config.runtime).run(config, toolset)

async def run_manager(config: AgentConfig) -> dict:
    """Run a manager agent on its configured runtime."""
    toolset = manager_toolset(system_prompt=build_manager_system_prompt(config))
    return await _get_executor(config.runtime).run(config, toolset)

async def run_worker_mock(config: AgentConfig) -> dict:
    """Mock worker for testing without any vendor SDK."""
    db = open_db(config.run_id)
    try:
        _ = update_agent_status(db, config.run_id, config.name, 'running')
        _ = insert_event(db, config.run_id, config.name, 'started', {'prompt': config.prompt[:200]})
        await asyncio.sleep(0.5)
        result = subprocess.run(config.check_command, shell=True, cwd=str(config.worktree), capture_output=True, text=True)
        if result.returncode == 0:
            _ = update_agent_status(db, config.run_id, config.name, 'completed')
            _ = insert_event(db, config.run_id, config.name, 'done', {'summary': 'Mock task completed'})
            return {'success': True, 'status': 'completed'}
        _ = update_agent_status(db, config.run_id, config.name, 'failed', result.stderr[:500])
        _ = insert_event(db, config.run_id, config.name, 'error', {'error': result.stderr[:200]})
        return {'success': False, 'status': 'failed', 'error': result.stderr[:500]}
    except Exception as e:
        logger.error(f"Mock worker {config.name} failed: {e}")
        _ = update_agent_status(db, config.run_id, config.name, 'failed', str(e))
        return {'success': False, 'status': 'failed', 'error': str(e)}
    finally:
        db.close()

def spawn_worker(config: AgentConfig, use_mock: bool=False) -> asyncio.Task:
    """Spawn a worker agent as an asyncio task."""
    if use_mock:
        return asyncio.create_task(run_worker_mock(config), name=f'worker-{config.name}')
    return asyncio.create_task(run_worker(config), name=f'worker-{config.name}')

def spawn_manager(config: AgentConfig) -> asyncio.Task:
    """Spawn a manager agent as an asyncio task."""
    return asyncio.create_task(run_manager(config), name=f'manager-{config.name}')
