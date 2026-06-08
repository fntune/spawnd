"""OpenAI Agents SDK executor."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging
from typing import TYPE_CHECKING

from spawnd.core.budget import estimate_cost_usd
from spawnd.runtime.executors.base import Executor, register
from spawnd.runtime.executors.refs import resolve_refs
from spawnd.tools.factory_openai import build_manager_coord_tools, build_worker_coord_tools
from spawnd.tools.openai_code import build_code_tools

if TYPE_CHECKING:
    from spawnd.runtime.agent_run import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.openai")
DEFAULT_OPENAI_MODEL = "gpt-5"


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
            code_tools = build_code_tools(config.worktree, write_allowed=toolset.write_allowed)
            model = config.model if config.model and config.model != "sonnet" else DEFAULT_OPENAI_MODEL
            starter = (
                "Execute the task. Spawn workers as needed. When all work is done, summarize the result."
                if is_manager
                else "Execute the task now. When done, summarize the result."
            )
            async with AsyncExitStack() as stack:
                mcp_servers = await _openai_mcp_servers(config, stack)
                conversation_id = await _openai_conversation_id(config.resume_session_id)
                agent = Agent(
                    name=config.name,
                    instructions=toolset.system_prompt,
                    tools=coord_tools + code_tools,
                    model=model,
                    mcp_servers=mcp_servers,
                )
                logger.info("Starting %s %s via OpenAI Agents SDK (%s)", role_label, config.name, model)
                result = await _run_openai_stream(
                    Runner,
                    agent,
                    f"{starter}\n\nTask: {config.prompt}",
                    max_turns=config.max_iterations,
                    conversation_id=conversation_id,
                )
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
                    "vendor_session_id": conversation_id,
                    "cost_source": "estimated",
                }
            return {
                "success": True,
                "status": "completed",
                "cost": total_cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "final_output": final_text,
                "vendor_session_id": conversation_id,
                "cost_source": "estimated",
            }
        except Exception as exc:
            logger.error("OpenAI %s %s failed: %s", role_label, config.name, exc)
            config.observer.error("openai_agents", str(exc))
            return {"success": False, "status": "failed", "error": str(exc), "cost": 0.0, "cost_source": "estimated"}


register(OpenAIExecutor())


async def _openai_conversation_id(existing_id: str | None) -> str:
    """Return a server-managed OpenAI conversation id for resumable runs."""

    if existing_id:
        return existing_id
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("openai is not installed") from exc
    conversation = await AsyncOpenAI().conversations.create()
    return str(conversation.id)


async def _run_openai_stream(
    runner: object,
    agent: object,
    prompt: str,
    *,
    max_turns: int,
    conversation_id: str,
) -> object:
    """Run OpenAI Agents through the streaming result so cancellation has a provider hook."""

    result = runner.run_streamed(
        agent,
        prompt,
        max_turns=max_turns,
        conversation_id=conversation_id,
    )
    try:
        async for _event in result.stream_events():
            pass
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            result.cancel("immediate")
            raise asyncio.CancelledError
        return result
    except asyncio.CancelledError:
        result.cancel("immediate")
        raise


async def _openai_mcp_servers(config: AgentConfig, stack: AsyncExitStack) -> list[object]:
    """Build and enter OpenAI Agents SDK MCP server contexts."""

    if not config.mcp_servers:
        return []
    try:
        from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
    except ImportError as exc:
        raise RuntimeError("OpenAI Agents SDK MCP support is not available") from exc

    servers: list[object] = []
    for spec in config.mcp_servers:
        if spec.type == "stdio":
            if not spec.command:
                raise ValueError(f"MCP server {spec.name} requires command")
            params: dict = {"command": spec.command}
            if spec.args:
                params["args"] = list(spec.args)
            env = resolve_refs(spec.env_refs, f"MCP server {spec.name} env")
            if env:
                params["env"] = env
            server = MCPServerStdio(name=spec.name, params=params)
        elif spec.type == "http":
            if not spec.url:
                raise ValueError(f"MCP server {spec.name} requires url")
            headers = dict(spec.headers)
            headers.update(resolve_refs(spec.header_refs, f"MCP server {spec.name} headers"))
            params = {"url": spec.url}
            if headers:
                params["headers"] = headers
            server = MCPServerStreamableHttp(name=spec.name, params=params)
        elif spec.type == "sse":
            if not spec.url:
                raise ValueError(f"MCP server {spec.name} requires url")
            headers = dict(spec.headers)
            headers.update(resolve_refs(spec.header_refs, f"MCP server {spec.name} headers"))
            params = {"url": spec.url}
            if headers:
                params["headers"] = headers
            server = MCPServerSse(name=spec.name, params=params)
        else:
            raise ValueError(f"Unsupported MCP server type: {spec.type}")
        servers.append(await stack.enter_async_context(server))
    return servers
