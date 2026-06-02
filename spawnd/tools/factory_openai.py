"""OpenAI function_tool wrappers for the coord tool set.

Same underlying implementations (``spawnd.tools.worker`` / ``manager``),
different wrapping — bare function_tool decorators so the OpenAI agent
gets plain string returns, no Claude content-block unwrap.
"""
from __future__ import annotations
from typing import Any
try:
    from agents import function_tool
except ImportError as err:
    raise ImportError("spawnd.tools.factory_openai requires the openai-agents SDK. Install with: pip install 'spawnd.dev[openai]'") from err
from spawnd.tools.coord import CoordToolSpec, MANAGER_TOOL_SPECS, WORKER_TOOL_SPECS, invoke_manager_tool, invoke_worker_tool

def _worker_tool(spec: CoordToolSpec, run_id: str, agent_name: str, *, parent: str, tree_path: str):
    if spec.name == 'mark_complete':

        @function_tool
        async def mark_complete(summary: str) -> str:
            kwargs = spec.build_kwargs({'summary': summary})
            return await invoke_worker_tool(spec, run_id, agent_name, kwargs, parent=parent, tree_path=tree_path)
        return mark_complete
    if spec.name == 'request_clarification':

        @function_tool
        async def request_clarification(question: str, escalate_to: str='auto') -> str:
            kwargs = spec.build_kwargs({'question': question, 'escalate_to': escalate_to})
            return await invoke_worker_tool(spec, run_id, agent_name, kwargs, parent=parent, tree_path=tree_path)
        return request_clarification
    if spec.name == 'report_progress':

        @function_tool
        async def report_progress(status: str, milestone: str | None=None) -> str:
            kwargs = spec.build_kwargs({'status': status, 'milestone': milestone})
            return await invoke_worker_tool(spec, run_id, agent_name, kwargs, parent=parent, tree_path=tree_path)
        return report_progress
    if spec.name == 'report_blocker':

        @function_tool
        async def report_blocker(issue: str) -> str:
            kwargs = spec.build_kwargs({'issue': issue})
            return await invoke_worker_tool(spec, run_id, agent_name, kwargs, parent=parent, tree_path=tree_path)
        return report_blocker
    raise ValueError(f'Unknown worker tool spec: {spec.name}')

def _manager_tool(spec: CoordToolSpec, run_id: str, manager_name: str):
    if spec.name == 'spawn_worker':

        @function_tool
        async def spawn_worker(name: str, prompt: str, check: str | None=None, model: str='sonnet') -> str:
            kwargs = spec.build_kwargs({'name': name, 'prompt': prompt, 'check': check, 'model': model})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return spawn_worker
    if spec.name == 'respond_to_clarification':

        @function_tool
        async def respond_to_clarification(clarification_id: str, response: str) -> str:
            kwargs = spec.build_kwargs({'clarification_id': clarification_id, 'response': response})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return respond_to_clarification
    if spec.name == 'cancel_worker':

        @function_tool
        async def cancel_worker(name: str) -> str:
            kwargs = spec.build_kwargs({'name': name})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return cancel_worker
    if spec.name == 'get_worker_status':

        @function_tool
        async def get_worker_status(name: str | None=None) -> str:
            kwargs = spec.build_kwargs({'name': name})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return get_worker_status
    if spec.name == 'get_pending_clarifications':

        @function_tool
        async def get_pending_clarifications() -> str:
            kwargs = spec.build_kwargs({})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return get_pending_clarifications
    if spec.name == 'mark_plan_complete':

        @function_tool
        async def mark_plan_complete(summary: str) -> str:
            kwargs = spec.build_kwargs({'summary': summary})
            return await invoke_manager_tool(spec, run_id, manager_name, kwargs)
        return mark_plan_complete
    raise ValueError(f'Unknown manager tool spec: {spec.name}')

def build_worker_coord_tools(run_id: str, agent_name: str, *, parent: str='', tree_path: str='') -> list[Any]:
    """Worker coord tools for an OpenAI agent."""
    return [_worker_tool(spec, run_id, agent_name, parent=parent, tree_path=tree_path) for spec in WORKER_TOOL_SPECS]

def build_manager_coord_tools(run_id: str, manager_name: str) -> list[Any]:
    """Manager coord tools for an OpenAI agent."""
    return [_manager_tool(spec, run_id, manager_name) for spec in MANAGER_TOOL_SPECS]
