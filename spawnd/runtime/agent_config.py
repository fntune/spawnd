"""Agent config resolution helpers for deployed execution boundaries."""
import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any
from spawnd.models.specs import AgentSpec, CodexRuntimeConfig, Defaults, McpServerSpec
from spawnd.roles import apply_role, get_role_defaults

@dataclass(frozen=True)
class ResolvedAgentPlanConfig:
    """Effective persisted config resolved from plan defaults + role + agent overrides."""
    prompt: str
    check_command: str | None
    model: str | None
    max_iterations: int
    runtime_timeout_seconds: int | None
    check_timeout_seconds: int | None
    max_cost_usd: float
    on_failure: str
    retry_count: int
    runtime: str
    cost_source: str
    manager_cap: int | None
    write_allowed: bool
    codex: CodexRuntimeConfig | None
    mcp_servers: list[McpServerSpec]

@dataclass(frozen=True)
class HydratedAgentRuntimeConfig:
    """Effective runtime config hydrated from an agent database row."""
    prompt: str
    check_command: str
    model: str | None
    max_iterations: int
    runtime_timeout_seconds: int | None
    check_timeout_seconds: int | None
    max_cost_usd: float
    runtime: str
    env: dict[str, str] | None
    write_allowed: bool
    codex: CodexRuntimeConfig | None
    mcp_servers: list[McpServerSpec]

def resolve_agent_plan_config(agent: AgentSpec, defaults: Defaults) -> ResolvedAgentPlanConfig:
    """Resolve effective persisted agent config from plan + role + agent inputs."""
    prompt = agent.prompt
    role_defaults: dict = {}
    if agent.use_role:
        prompt = apply_role(agent.prompt, agent.use_role)
        role_defaults = get_role_defaults(agent.use_role)
    check_command = agent.check if agent.check is not None else role_defaults.get('check')
    if check_command is None:
        check_command = defaults.check
    runtime = agent.runtime or defaults.runtime
    model = agent.model if agent.model is not None else role_defaults.get('model')
    if model is None:
        if runtime == 'codex' and defaults.model == 'sonnet':
            model = None
        else:
            model = defaults.model
    manager_cap = agent.manager.max_subagents if agent.type == 'manager' and agent.manager is not None else None
    write_allowed = agent.write_allowed if agent.write_allowed is not None else agent.use_role != 'reviewer'
    return ResolvedAgentPlanConfig(
        prompt=prompt,
        check_command=check_command,
        model=model,
        max_iterations=agent.max_iterations if agent.max_iterations is not None else defaults.max_iterations,
        runtime_timeout_seconds=agent.runtime_timeout_seconds
        if agent.runtime_timeout_seconds is not None
        else defaults.runtime_timeout_seconds,
        check_timeout_seconds=agent.check_timeout_seconds
        if agent.check_timeout_seconds is not None
        else defaults.check_timeout_seconds,
        max_cost_usd=agent.max_cost_usd if agent.max_cost_usd is not None else defaults.max_cost_usd,
        on_failure=agent.on_failure if agent.on_failure is not None else defaults.on_failure,
        retry_count=agent.retry_count if agent.retry_count is not None else defaults.retry_count,
        runtime=runtime,
        cost_source='estimated' if runtime == 'openai' else 'codex' if runtime == 'codex' else 'sdk',
        manager_cap=manager_cap,
        write_allowed=write_allowed,
        codex=agent.codex if agent.codex is not None else defaults.codex,
        mcp_servers=[*defaults.mcp_servers, *agent.mcp_servers],
    )

def hydrate_agent_runtime_config(agent_row: Mapping[str, Any], prompt: str) -> HydratedAgentRuntimeConfig:
    """Hydrate runtime config from a deployed agent record."""
    env_raw = agent_row.get('env')
    runtime = agent_row.get('runtime') or 'claude'
    stored_model = agent_row.get('model')
    model = stored_model if stored_model is not None else None if runtime == 'codex' else 'sonnet'
    check_command = agent_row.get('check_command') or agent_row.get('check_command_preview') or 'true'
    max_iterations = agent_row.get('max_iterations') if agent_row.get('max_iterations') is not None else 30
    runtime_timeout_seconds = agent_row.get('runtime_timeout_seconds')
    check_timeout_seconds = agent_row.get('check_timeout_seconds')
    max_cost_usd = agent_row.get('max_cost_usd') if agent_row.get('max_cost_usd') is not None else 5.0
    write_allowed = bool(agent_row.get('write_allowed')) if agent_row.get('write_allowed') is not None else True
    if isinstance(env_raw, str):
        env = json.loads(env_raw) if env_raw else None
    elif isinstance(env_raw, dict):
        env = env_raw
    else:
        env = None
    return HydratedAgentRuntimeConfig(
        prompt=prompt,
        check_command=check_command,
        model=model,
        max_iterations=max_iterations,
        runtime_timeout_seconds=runtime_timeout_seconds,
        check_timeout_seconds=check_timeout_seconds,
        max_cost_usd=max_cost_usd,
        runtime=runtime,
        env=env,
        write_allowed=write_allowed,
        codex=None,
        mcp_servers=[],
    )
