"""Toolset — vendor-neutral description of which tools an agent may use.

The same ``Toolset`` is consumed by every executor: the Claude executor
expands it into ``ClaudeAgentOptions.allowed_tools + mcp_servers``, the
OpenAI executor expands it into ``Agent(tools=[...])``. Manager vs worker
is a toolset difference (which coord ops are allowed), not an executor
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Default "coding" tools for Claude (the MCP-native names).
DEFAULT_CODE_TOOLS: list[str] = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
]

READONLY_CODE_TOOLS: list[str] = ["Read", "Glob", "Grep"]

WORKER_COORD_OPS: list[str] = [
    "mark_complete",
    "request_clarification",
    "report_progress",
    "report_blocker",
]

MANAGER_COORD_OPS: list[str] = [
    "spawn_worker",
    "respond_to_clarification",
    "cancel_worker",
    "get_worker_status",
    "get_pending_clarifications",
    "mark_plan_complete",
]


@dataclass(frozen=True)
class Toolset:
    """Which tools an agent may use.

    Attributes:
        coord: vendor-neutral coord op names this agent can call
            (e.g. ``["mark_complete", "report_progress"]``).
        code: code-manipulation tool names in each vendor's native
            vocabulary (Claude uses ``Read/Write/Edit/...``; OpenAI
            wrappers recognise the same names and map them to
            ``@function_tool``-decorated closures).
        write_allowed: when False, executors strip write/edit/shell
            tools from the expanded tool list (e.g. Claude sets
            ``permission_mode="plan"``).
        system_prompt: free-form system prompt prefix the executor
            should prepend to the agent's task.
        role: optional role name (``implementer`` / ``reviewer`` / ...)
            for logging and introspection.
    """

    coord: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=lambda: list(DEFAULT_CODE_TOOLS))
    write_allowed: bool = True
    system_prompt: str = ""
    role: str | None = None


def worker_toolset(*, write_allowed: bool = True, system_prompt: str = "", role: str | None = None) -> Toolset:
    """Default toolset for a worker agent."""
    code = list(DEFAULT_CODE_TOOLS) if write_allowed else list(READONLY_CODE_TOOLS)
    return Toolset(
        coord=list(WORKER_COORD_OPS),
        code=code,
        write_allowed=write_allowed,
        system_prompt=system_prompt,
        role=role,
    )


def manager_toolset(*, system_prompt: str = "", role: str | None = None) -> Toolset:
    """Default toolset for a manager agent.

    Managers get the code tools (so they can read state themselves) plus
    the manager coord ops for spawning / steering workers.
    """
    return Toolset(
        coord=list(MANAGER_COORD_OPS),
        code=list(DEFAULT_CODE_TOOLS),
        write_allowed=True,
        system_prompt=system_prompt,
        role=role,
    )
