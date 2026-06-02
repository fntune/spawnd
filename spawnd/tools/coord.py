"""Shared coordination tool metadata and invocation adapters."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from spawnd.tools import manager, worker

@dataclass(frozen=True)
class CoordToolSpec:
    name: str
    description: str
    claude_schema: dict[str, type]
    build_kwargs: Callable[[dict], dict]
    worker_fn: Callable[..., Awaitable[str]]
    manager_fn: Callable[..., Awaitable[str]]
WORKER_TOOL_SPECS: tuple[CoordToolSpec, ...] = (CoordToolSpec(name='mark_complete', description='Signal task completion. Runs check command automatically.', claude_schema={'summary': str}, build_kwargs=lambda args: {'summary': args['summary']}, worker_fn=worker.mark_complete, manager_fn=worker.mark_complete), CoordToolSpec(name='request_clarification', description='Ask manager for guidance. BLOCKS until response.', claude_schema={'question': str, 'escalate_to': str}, build_kwargs=lambda args: {'question': args['question'], 'escalate_to': args.get('escalate_to', 'auto')}, worker_fn=worker.request_clarification, manager_fn=worker.request_clarification), CoordToolSpec(name='report_progress', description='Report progress update.', claude_schema={'status': str, 'milestone': str}, build_kwargs=lambda args: {'status': args['status'], 'milestone': args.get('milestone')}, worker_fn=worker.report_progress, manager_fn=worker.report_progress), CoordToolSpec(name='report_blocker', description='Report blocking issue. BLOCKS until resolved.', claude_schema={'issue': str}, build_kwargs=lambda args: {'issue': args['issue']}, worker_fn=worker.report_blocker, manager_fn=worker.report_blocker))
MANAGER_TOOL_SPECS: tuple[CoordToolSpec, ...] = (CoordToolSpec(name='spawn_worker', description='Spawn a new worker agent.', claude_schema={'name': str, 'prompt': str, 'check': str, 'model': str}, build_kwargs=lambda args: {'name': args['name'], 'prompt': args['prompt'], 'check': args.get('check'), 'model': args.get('model', 'sonnet')}, worker_fn=manager.spawn_worker, manager_fn=manager.spawn_worker), CoordToolSpec(name='respond_to_clarification', description="Respond to worker's clarification.", claude_schema={'clarification_id': str, 'response': str}, build_kwargs=lambda args: {'clarification_id': args['clarification_id'], 'response': args['response']}, worker_fn=manager.respond_to_clarification, manager_fn=manager.respond_to_clarification), CoordToolSpec(name='cancel_worker', description='Cancel a worker agent.', claude_schema={'name': str}, build_kwargs=lambda args: {'name': args['name']}, worker_fn=manager.cancel_worker, manager_fn=manager.cancel_worker), CoordToolSpec(name='get_worker_status', description='Get status of workers.', claude_schema={'name': str}, build_kwargs=lambda args: {'name': args.get('name')}, worker_fn=manager.get_worker_status, manager_fn=manager.get_worker_status), CoordToolSpec(name='get_pending_clarifications', description='Get pending clarifications from workers.', claude_schema={}, build_kwargs=lambda args: {}, worker_fn=manager.get_pending_clarifications, manager_fn=manager.get_pending_clarifications), CoordToolSpec(name='mark_plan_complete', description='Signal plan completion.', claude_schema={'summary': str}, build_kwargs=lambda args: {'summary': args['summary']}, worker_fn=manager.mark_plan_complete, manager_fn=manager.mark_plan_complete))

def invoke_worker_tool(spec: CoordToolSpec, run_id: str, agent_name: str, kwargs: dict[str, Any], *, parent: str, tree_path: str) -> Awaitable[str]:
    if spec.name in {'request_clarification', 'report_blocker'}:
        kwargs = {**kwargs, 'parent': parent, 'tree_path': tree_path}
    return spec.worker_fn(run_id, agent_name, **kwargs)

def invoke_manager_tool(spec: CoordToolSpec, run_id: str, manager_name: str, kwargs: dict[str, Any]) -> Awaitable[str]:
    return spec.manager_fn(run_id, manager_name, **kwargs)
