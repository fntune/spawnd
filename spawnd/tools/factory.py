"""Claude SDK MCP-tool factory.

Wraps the vendor-neutral coord tool functions (which return plain strings)
into Claude's ``@tool``-decorated content-block shape.
"""

from claude_agent_sdk import tool

from spawnd.tools.coord import (
    CoordToolSpec,
    MANAGER_TOOL_SPECS,
    WORKER_TOOL_SPECS,
    invoke_manager_tool,
    invoke_worker_tool,
)


def _wrap(text: str) -> dict:
    """Wrap a plain string return into Claude's MCP content-block shape."""
    return {"content": [{"type": "text", "text": text}]}


def create_worker_tools(run_id: str, agent_name: str, *, parent: str = "", tree_path: str = ""):
    """Create worker coordination tools as SDK MCP tools with captured context.

    ``parent`` and ``tree_path`` identify the agent's place in the manager/worker
    hierarchy. They are threaded through each closure so workers running
    concurrently in the same process don't clobber each other via ``os.environ``.
    """

    tools = []
    for spec in WORKER_TOOL_SPECS:
        async def handler(args: dict, _spec: CoordToolSpec = spec) -> dict:
            kwargs = _spec.build_kwargs(args)
            text = await invoke_worker_tool(
                _spec,
                run_id,
                agent_name,
                kwargs,
                parent=parent,
                tree_path=tree_path,
            )
            return _wrap(text)

        tools.append(tool(spec.name, spec.description, spec.claude_schema)(handler))
    return tools


def create_manager_tools(run_id: str, manager_name: str):
    """Create manager coordination tools as SDK MCP tools with captured context."""

    tools = []
    for spec in MANAGER_TOOL_SPECS:
        async def handler(args: dict, _spec: CoordToolSpec = spec) -> dict:
            kwargs = _spec.build_kwargs(args)
            text = await invoke_manager_tool(_spec, run_id, manager_name, kwargs)
            return _wrap(text)

        tools.append(tool(spec.name, spec.description, spec.claude_schema)(handler))
    return tools
