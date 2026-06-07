"""Claude Agent SDK executor."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from spawnd.runtime.executors.base import Executor, register
from spawnd.tools.factory import create_manager_tools, create_worker_tools

if TYPE_CHECKING:
    from spawnd.runtime.agent_run import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.claude")


class ClaudeExecutor(Executor):
    """Drive a Claude agent and report provider facts through the observer."""

    runtime = "claude"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                TextBlock,
                create_sdk_mcp_server,
            )
        except ImportError as exc:
            message = "claude_agent_sdk is not installed"
            config.observer.error("claude_sdk", message)
            return {"success": False, "status": "failed", "error": message, "cost": 0.0, "cost_source": "sdk"}

        is_manager = "mark_plan_complete" in toolset.coord
        role_label = "manager" if is_manager else "worker"
        try:
            config.observer.event("started", {"runtime": "claude", "role": role_label})
            coord_tools = (
                create_manager_tools(config.run_id, config.name)
                if is_manager
                else create_worker_tools(
                    config.run_id,
                    config.name,
                    parent=config.parent or "",
                    tree_path=config.tree_path(),
                )
            )
            server = create_sdk_mcp_server("spawnd", "1.0.0", coord_tools)
            allowed_tools = list(toolset.code) + [f"mcp__spawnd__{op}" for op in toolset.coord]
            options = ClaudeAgentOptions(
                cwd=str(config.worktree),
                env=config.execution_env(),
                mcp_servers={"spawnd": server},
                allowed_tools=allowed_tools,
                model=config.model,
                max_turns=config.max_iterations,
                permission_mode="bypassPermissions" if toolset.write_allowed else "plan",
                system_prompt=toolset.system_prompt,
            )
            starter = (
                "Execute the task. Spawn workers as needed. When all work is done, summarize the result."
                if is_manager
                else "Execute the task now. When done, summarize the result."
            )
            total_cost = 0.0
            iteration = 0
            session_id = None
            final_messages: list[str] = []
            logger.info("Starting %s %s via Claude SDK", role_label, config.name)
            async with ClaudeSDKClient(options=options) as client:
                await client.query(f"{starter}\n\nTask: {config.prompt}")
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        iteration += 1
                        config.observer.invocation("assistant_message", {"iteration": iteration})
                        for block in message.content or []:
                            if isinstance(block, TextBlock):
                                final_messages.append(block.text)
                                config.observer.final(block.text)
                    if isinstance(message, ResultMessage):
                        session_id = message.session_id
                        total_cost = message.total_cost_usd or 0.0
                        break
            final_text = "\n".join(final_messages[-3:])
            config.observer.usage(cost_usd=total_cost, source="sdk", raw={"iterations": iteration})
            if total_cost > config.max_cost_usd:
                message = f"Cost exceeded: ${total_cost:.4f}"
                config.observer.error("claude_sdk", message, {"budget": config.max_cost_usd})
                return {
                    "success": False,
                    "status": "cost_exceeded",
                    "cost": total_cost,
                    "error": message,
                    "vendor_session_id": session_id,
                    "final_message": final_text,
                    "cost_source": "sdk",
                }
            return {
                "success": True,
                "status": "completed",
                "cost": total_cost,
                "vendor_session_id": session_id,
                "final_message": final_text,
                "cost_source": "sdk",
            }
        except Exception as exc:
            logger.error("Claude %s %s failed: %s", role_label, config.name, exc)
            config.observer.error("claude_sdk", str(exc))
            return {"success": False, "status": "failed", "error": str(exc), "cost": 0.0, "cost_source": "sdk"}


register(ClaudeExecutor())
