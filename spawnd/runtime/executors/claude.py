"""Claude Agent SDK executor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

from spawnd.runtime.executors.base import Executor, register
from spawnd.storage.db import (
    get_agent,
    insert_event,
    open_db,
    update_agent_cost,
    update_agent_iteration,
    update_agent_status,
)
from spawnd.storage.paths import ensure_log_file
from spawnd.tools.factory import create_manager_tools, create_worker_tools

if TYPE_CHECKING:
    from spawnd.runtime.executor import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.claude")


def _build_agent_env(config: AgentConfig) -> dict[str, str]:
    """Build environment variables for the subprocess Claude agent."""
    env = {
        "SPAWND_RUN_ID": config.run_id,
        "SPAWND_AGENT_NAME": config.name,
        "SPAWND_PARENT_AGENT": config.parent or "",
        "SPAWND_TREE_PATH": config.tree_path(),
    }
    if config.env:
        env.update(config.env)
    return env


class ClaudeExecutor(Executor):
    """Drive a Claude agent via the Claude Agent SDK."""

    runtime = "claude"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        db = open_db(config.run_id)
        is_manager = "mark_plan_complete" in toolset.coord
        role_label = "Manager" if is_manager else "Worker"

        try:
            update_agent_status(db, config.run_id, config.name, "running")
            insert_event(db, config.run_id, config.name, "started", {"prompt": config.prompt[:200]})

            if is_manager:
                coord_tools = create_manager_tools(config.run_id, config.name)
            else:
                coord_tools = create_worker_tools(
                    config.run_id,
                    config.name,
                    parent=config.parent or "",
                    tree_path=config.tree_path(),
                )
            server = create_sdk_mcp_server("spawnd", "1.0.0", coord_tools)

            allowed_tools = list(toolset.code) + [f"mcp__spawnd__{op}" for op in toolset.coord]
            permission_mode = "plan" if not toolset.write_allowed else "bypassPermissions"

            options = ClaudeAgentOptions(
                cwd=str(config.worktree),
                env=_build_agent_env(config),
                mcp_servers={"spawnd": server},
                allowed_tools=allowed_tools,
                model=config.model,
                max_turns=config.max_iterations,
                permission_mode=permission_mode,
                system_prompt=toolset.system_prompt,
            )

            logger.info(f"Starting {role_label.lower()} {config.name} in {config.worktree}")
            log_path = ensure_log_file(config.run_id, config.name)

            starter = (
                "Execute the task. Spawn workers as needed. When all work is done, call mark_plan_complete."
                if is_manager
                else "Execute the task now. When done, call mark_complete with a summary."
            )

            session_id = None
            total_cost = 0.0
            iteration = 0

            async with ClaudeSDKClient(options=options) as client:
                await client.query(f"{starter}\n\nTask: {config.prompt}")

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        iteration += 1
                        update_agent_iteration(db, config.run_id, config.name, iteration)

                        with open(log_path, "a") as f:
                            for block in message.content or []:
                                if isinstance(block, TextBlock):
                                    f.write(block.text + "\n")

                    if isinstance(message, ResultMessage):
                        session_id = message.session_id
                        total_cost = message.total_cost_usd or 0.0
                        break

            update_agent_cost(db, config.run_id, config.name, total_cost)

            if total_cost > config.max_cost_usd:
                logger.warning(
                    f"{role_label} {config.name} exceeded cost budget "
                    f"(${total_cost:.4f} > ${config.max_cost_usd:.2f})"
                )
                update_agent_status(
                    db, config.run_id, config.name, "cost_exceeded",
                    f"Cost exceeded: ${total_cost:.4f}",
                )
                insert_event(
                    db, config.run_id, config.name, "error",
                    {"error": "cost_exceeded", "cost": total_cost, "budget": config.max_cost_usd},
                )
                return {
                    "success": False,
                    "status": "cost_exceeded",
                    "cost": total_cost,
                    "error": "cost_exceeded",
                }

            agent = get_agent(db, config.run_id, config.name)
            final_status = agent["status"] if agent else "unknown"

            if final_status == "completed":
                logger.info(f"{role_label} {config.name} completed (cost: ${total_cost:.4f})")
                return {
                    "success": True,
                    "status": "completed",
                    "cost": total_cost,
                    "vendor_session_id": session_id,
                }

            if final_status in ("failed", "timeout", "cancelled", "cost_exceeded"):
                logger.warning(f"{role_label} {config.name} ended with status: {final_status}")
                return {"success": False, "status": final_status, "cost": total_cost}

            update_agent_status(
                db, config.run_id, config.name, "timeout",
                "Max iterations without completion" if not is_manager else "Max iterations",
            )
            logger.warning(f"{role_label} {config.name} timed out")
            return {"success": False, "status": "timeout", "cost": total_cost}

        except Exception as e:
            logger.error(f"{role_label} {config.name} failed: {e}")
            update_agent_status(db, config.run_id, config.name, "failed", str(e))
            insert_event(db, config.run_id, config.name, "error", {"error": str(e)})
            return {"success": False, "status": "failed", "error": str(e)}

        finally:
            db.close()


register(ClaudeExecutor())
