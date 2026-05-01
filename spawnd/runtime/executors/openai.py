"""OpenAI Agents SDK executor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:  # pragma: no cover
    from agents import Agent, Runner
except ImportError as err:  # pragma: no cover
    raise ImportError(
        "spawnd.runtime.executors.openai requires the openai-agents SDK. "
        "Install with: pip install 'spawnd.dev[openai]'"
    ) from err

from spawnd.core.budget import estimate_cost_usd
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
from spawnd.tools.factory_openai import build_manager_coord_tools, build_worker_coord_tools
from spawnd.tools.openai_code import build_code_tools

if TYPE_CHECKING:
    from spawnd.runtime.executor import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.openai")

DEFAULT_OPENAI_MODEL = "gpt-5"


def _build_agent_env(config: AgentConfig) -> dict[str, str]:
    """Build environment variables exposed to OpenAI code tools."""
    env = {
        "SPAWND_RUN_ID": config.run_id,
        "SPAWND_AGENT_NAME": config.name,
        "SPAWND_PARENT_AGENT": config.parent or "",
        "SPAWND_TREE_PATH": config.tree_path(),
    }
    if config.env:
        env.update(config.env)
    return env


class OpenAIExecutor(Executor):
    """Drive an agent via the OpenAI Agents SDK (``openai-agents``)."""

    runtime = "openai"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        db = open_db(config.run_id)
        is_manager = "mark_plan_complete" in toolset.coord
        role_label = "Manager" if is_manager else "Worker"

        try:
            update_agent_status(db, config.run_id, config.name, "running")
            insert_event(db, config.run_id, config.name, "started", {"prompt": config.prompt[:200]})

            # Build tools: coord + code (write-gated by toolset).
            if is_manager:
                coord_tools = build_manager_coord_tools(config.run_id, config.name)
            else:
                coord_tools = build_worker_coord_tools(
                    config.run_id,
                    config.name,
                    parent=config.parent or "",
                    tree_path=config.tree_path(),
                )
            code_tools = build_code_tools(
                config.worktree,
                write_allowed=toolset.write_allowed,
                env=_build_agent_env(config),
            )

            model = config.model if config.model and config.model != "sonnet" else DEFAULT_OPENAI_MODEL

            agent = Agent(
                name=config.name,
                instructions=toolset.system_prompt,
                tools=coord_tools + code_tools,
                model=model,
            )

            starter = (
                "Execute the task. Spawn workers as needed. When all work is done, call mark_plan_complete."
                if is_manager
                else "Execute the task now. When done, call mark_complete with a summary."
            )
            prompt = f"{starter}\n\nTask: {config.prompt}"

            logger.info(f"Starting {role_label.lower()} {config.name} on OpenAI ({model})")
            log_path = ensure_log_file(config.run_id, config.name)

            result = await Runner.run(agent, prompt, max_turns=config.max_iterations)

            # Aggregate token usage + iteration count.
            input_tokens = sum(getattr(r.usage, "input_tokens", 0) or 0 for r in result.raw_responses)
            output_tokens = sum(getattr(r.usage, "output_tokens", 0) or 0 for r in result.raw_responses)
            iteration = len(result.raw_responses)
            total_cost = estimate_cost_usd(model, input_tokens, output_tokens)

            update_agent_iteration(db, config.run_id, config.name, iteration)
            update_agent_cost(db, config.run_id, config.name, total_cost)

            if result.final_output:
                with open(log_path, "a") as f:
                    f.write(str(result.final_output) + "\n")

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

            agent_row = get_agent(db, config.run_id, config.name)
            final_status = agent_row["status"] if agent_row else "unknown"

            if final_status == "completed":
                logger.info(f"{role_label} {config.name} completed (estimated cost: ${total_cost:.4f})")
                return {"success": True, "status": "completed", "cost": total_cost}

            if final_status in ("failed", "timeout", "cancelled", "cost_exceeded"):
                logger.warning(f"{role_label} {config.name} ended with status: {final_status}")
                return {"success": False, "status": final_status, "cost": total_cost}

            update_agent_status(
                db, config.run_id, config.name, "timeout",
                "Max iterations without completion",
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


register(OpenAIExecutor())
