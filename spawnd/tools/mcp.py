"""Stdio MCP server exposing Spawnd coordination tools."""
from __future__ import annotations

import os

from spawnd.tools import manager, worker


def build_server():
    """Build a FastMCP server for the current agent context."""

    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("fastmcp is required for spawnd.tools.mcp") from exc

    run_id = _required_env('SPAWND_RUN_ID')
    agent_name = _required_env('SPAWND_AGENT_NAME')
    agent_type = os.environ.get('SPAWND_AGENT_TYPE', 'worker')
    parent = os.environ.get('SPAWND_PARENT_AGENT', '')
    tree_path = os.environ.get('SPAWND_TREE_PATH', agent_name)
    server = FastMCP(name='spawnd')
    if agent_type == 'manager':
        _register_manager_tools(server, run_id, agent_name)
    else:
        _register_worker_tools(server, run_id, agent_name, parent=parent, tree_path=tree_path)
    return server


def _register_worker_tools(server, run_id: str, agent_name: str, *, parent: str, tree_path: str) -> None:
    @server.tool
    async def mark_complete(summary: str) -> str:
        """Signal task completion. The deployed worker will verify before finalizing."""

        return await worker.mark_complete(run_id, agent_name, summary)

    @server.tool
    async def request_clarification(question: str, escalate_to: str = 'auto') -> str:
        """Ask the manager for clarification and wait for a response."""

        return await worker.request_clarification(
            run_id,
            agent_name,
            question,
            escalate_to=escalate_to,
            parent=parent,
            tree_path=tree_path,
        )

    @server.tool
    async def report_progress(status: str, milestone: str | None = None) -> str:
        """Report progress for this worker."""

        return await worker.report_progress(run_id, agent_name, status, milestone=milestone)

    @server.tool
    async def report_blocker(issue: str) -> str:
        """Report a blocker and wait for manager guidance."""

        return await worker.report_blocker(run_id, agent_name, issue, parent=parent, tree_path=tree_path)


def _register_manager_tools(server, run_id: str, manager_name: str) -> None:
    @server.tool
    async def spawn_worker(name: str, prompt: str, check: str | None = None, model: str = 'sonnet') -> str:
        """Spawn a durable queued worker agent."""

        return await manager.spawn_worker(run_id, manager_name, name, prompt, check=check, model=model)

    @server.tool
    async def respond_to_clarification(clarification_id: str, response: str) -> str:
        """Respond to a pending worker clarification or blocker."""

        return await manager.respond_to_clarification(run_id, manager_name, clarification_id, response)

    @server.tool
    async def cancel_worker(name: str) -> str:
        """Cancel a managed worker agent."""

        return await manager.cancel_worker(run_id, manager_name, name)

    @server.tool
    async def get_worker_status(name: str | None = None) -> str:
        """Return status for one managed worker, or all managed workers."""

        return await manager.get_worker_status(run_id, manager_name, name)

    @server.tool
    async def get_pending_clarifications() -> str:
        """Return pending worker clarifications and blockers."""

        return await manager.get_pending_clarifications(run_id, manager_name)

    @server.tool
    async def mark_plan_complete(summary: str) -> str:
        """Mark the manager plan complete when managed workers are done."""

        return await manager.mark_plan_complete(run_id, manager_name, summary)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'{name} is required for spawnd MCP coordination server')
    return value


def main() -> None:
    build_server().run(transport='stdio', show_banner=False)


if __name__ == '__main__':
    main()
