"""Agent execution dispatch for deployed workers."""
import asyncio
import logging
from spawnd.runtime.agent_run import AgentConfig
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
    toolset = worker_toolset(system_prompt=build_system_prompt(config), write_allowed=config.write_allowed)
    return await _get_executor(config.runtime).run(config, toolset)

async def run_manager(config: AgentConfig) -> dict:
    """Run a manager agent on its configured runtime."""
    toolset = manager_toolset(system_prompt=build_manager_system_prompt(config))
    return await _get_executor(config.runtime).run(config, toolset)

async def run_worker_mock(config: AgentConfig) -> dict:
    """Mock worker for testing without any vendor SDK."""
    try:
        config.observer.event('started', {'runtime': 'fake'})
        await asyncio.sleep(0.05)
        message = f"Fake runtime completed {config.name}"
        config.observer.final(message)
        config.observer.usage(source='fake')
        return {'success': True, 'status': 'completed', 'final_message': message, 'cost': 0.0, 'cost_source': 'fake'}
    except Exception as e:
        logger.error(f"Fake worker {config.name} failed: {e}")
        config.observer.error('fake_runtime', str(e))
        return {'success': False, 'status': 'failed', 'error': str(e), 'cost': 0.0, 'cost_source': 'fake'}
