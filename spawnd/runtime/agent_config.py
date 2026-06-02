"""Agent config resolution helpers for scheduler/executor boundaries."""
import json
import sqlite3
from dataclasses import dataclass
from spawnd.models.specs import AgentSpec, Defaults
from spawnd.roles import apply_role, get_role_defaults

@dataclass(frozen=True)
class ResolvedAgentPlanConfig:
    """Effective persisted config resolved from plan defaults + role + agent overrides."""
    prompt: str
    check_command: str | None
    model: str | None
    max_iterations: int
    max_cost_usd: float
    on_failure: str
    retry_count: int
    runtime: str
    cost_source: str
    manager_cap: int | None

@dataclass(frozen=True)
class HydratedAgentRuntimeConfig:
    """Effective runtime config hydrated from an agent database row."""
    prompt: str
    check_command: str
    model: str
    max_iterations: int
    max_cost_usd: float
    runtime: str
    env: dict[str, str] | None

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
    model = agent.model if agent.model is not None else role_defaults.get('model')
    if model is None:
        model = defaults.model
    runtime = agent.runtime or defaults.runtime
    manager_cap = agent.manager.max_subagents if agent.type == 'manager' and agent.manager is not None else None
    return ResolvedAgentPlanConfig(prompt=prompt, check_command=check_command, model=model, max_iterations=agent.max_iterations if agent.max_iterations is not None else defaults.max_iterations, max_cost_usd=agent.max_cost_usd if agent.max_cost_usd is not None else defaults.max_cost_usd, on_failure=agent.on_failure if agent.on_failure is not None else defaults.on_failure, retry_count=agent.retry_count if agent.retry_count is not None else defaults.retry_count, runtime=runtime, cost_source='estimated' if runtime == 'openai' else 'codex-cli' if runtime == 'codex' else 'sdk', manager_cap=manager_cap)

def hydrate_agent_runtime_config(agent_row: sqlite3.Row, prompt: str) -> HydratedAgentRuntimeConfig:
    """Hydrate runtime config from persisted DB row with backwards-compatible fallbacks."""
    env_raw = None
    try:
        env_raw = agent_row['env']
    except (IndexError, KeyError):
        env_raw = None
    try:
        runtime = agent_row['runtime'] or 'claude'
    except (IndexError, KeyError):
        runtime = 'claude'
    return HydratedAgentRuntimeConfig(prompt=prompt, check_command=agent_row['check_command'] if agent_row['check_command'] is not None else 'true', model=agent_row['model'] if agent_row['model'] is not None else 'sonnet', max_iterations=agent_row['max_iterations'] if agent_row['max_iterations'] is not None else 30, max_cost_usd=agent_row['max_cost_usd'] if agent_row['max_cost_usd'] is not None else 5.0, runtime=runtime, env=json.loads(env_raw) if env_raw else None)
