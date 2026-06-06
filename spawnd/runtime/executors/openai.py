"""OpenAI Agents SDK executor."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from spawnd.core.budget import estimate_cost_usd
from spawnd.runtime.executors.base import Executor, register
from spawnd.tools.factory_openai import build_manager_coord_tools, build_worker_coord_tools
from spawnd.tools.openai_code import build_code_tools

if TYPE_CHECKING:
    from spawnd.runtime.agent_run import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.openai")
DEFAULT_OPENAI_MODEL = "gpt-5"


def _build_agent_env(config: AgentConfig) -> dict[str, str]:
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
    """Drive an agent via the OpenAI Agents SDK."""

    runtime = "openai"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        try:
            from agents import Agent, Runner
        except ImportError:
            message = "openai-agents is not installed"
            config.observer.error("openai_agents", message)
            return {"success": False, "status": "failed", "error": message, "cost": 0.0, "cost_source": "estimated"}

        is_manager = "mark_plan_complete" in toolset.coord
        role_label = "manager" if is_manager else "worker"
        try:
            config.observer.event("started", {"runtime": "openai", "role": role_label})
            coord_tools = (
                build_manager_coord_tools(config.run_id, config.name)
                if is_manager
                else build_worker_coord_tools(
                    config.run_id,
                    config.name,
                    parent=config.parent or "",
                    tree_path=config.tree_path(),
                )
            )
            code_tools = build_code_tools(config.worktree, write_allowed=toolset.write_allowed, env=_build_agent_env(config))
            model = config.model if config.model and config.model != "sonnet" else DEFAULT_OPENAI_MODEL
            agent = Agent(name=config.name, instructions=toolset.system_prompt, tools=coord_tools + code_tools, model=model)
            starter = (
                "Execute the task. Spawn workers as needed. When all work is done, summarize the result."
                if is_manager
                else "Execute the task now. When done, summarize the result."
            )
            logger.info("Starting %s %s via OpenAI Agents SDK (%s)", role_label, config.name, model)
            result = await Runner.run(agent, f"{starter}\n\nTask: {config.prompt}", max_turns=config.max_iterations)
            input_tokens = sum((getattr(r.usage, "input_tokens", 0) or 0 for r in result.raw_responses))
            output_tokens = sum((getattr(r.usage, "output_tokens", 0) or 0 for r in result.raw_responses))
            total_cost = estimate_cost_usd(model, input_tokens, output_tokens)
            final_text = str(result.final_output or "")
            config.observer.final(final_text)
            config.observer.usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=total_cost,
                source="estimated",
                raw={"responses": len(result.raw_responses)},
            )
            if total_cost > config.max_cost_usd:
                message = f"Cost exceeded: ${total_cost:.4f}"
                config.observer.error("openai_agents", message, {"budget": config.max_cost_usd})
                return {
                    "success": False,
                    "status": "cost_exceeded",
                    "cost": total_cost,
                    "error": message,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "final_output": final_text,
                    "cost_source": "estimated",
                }
            return {
                "success": True,
                "status": "completed",
                "cost": total_cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "final_output": final_text,
                "cost_source": "estimated",
            }
        except Exception as exc:
            logger.error("OpenAI %s %s failed: %s", role_label, config.name, exc)
            config.observer.error("openai_agents", str(exc))
            return {"success": False, "status": "failed", "error": str(exc), "cost": 0.0, "cost_source": "estimated"}


register(OpenAIExecutor())
