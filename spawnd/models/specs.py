"""Input specification models for spawnd.dev."""
from typing import Literal
from pydantic import BaseModel, Field
AGENT_NAME_PATTERN = '^[A-Za-z0-9_.-]+$'
RuntimeName = Literal['claude', 'openai', 'codex']

class CodexRuntimeConfig(BaseModel):
    """Codex runtime safety and engine settings."""
    engine: Literal['auto', 'sdk', 'cli'] = 'auto'
    sandbox: Literal['read-only', 'workspace-write', 'danger-full-access'] = 'workspace-write'
    approval_mode: Literal['deny_all', 'auto_review'] = 'deny_all'
    ephemeral: bool = True
    dangerous_bypass: bool = False

class McpServerSpec(BaseModel):
    """External MCP server available to an agent runtime."""
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    type: Literal['stdio', 'http', 'sse'] = 'stdio'
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env_refs: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)

class RunConfig(BaseModel):
    """Run identity and resumption settings."""
    id: str | None = None
    resume: bool = False

class Defaults(BaseModel):
    """Plan-level default settings."""
    max_iterations: int = 30
    runtime_timeout_seconds: int | None = Field(default=None, gt=0)
    check_timeout_seconds: int | None = Field(default=300, gt=0)
    check: str | None = 'true'
    on_failure: Literal['continue', 'stop', 'retry'] = 'continue'
    retry_count: int = 3
    model: str = 'sonnet'
    max_cost_usd: float = 5.0
    runtime: RuntimeName = 'claude'
    codex: CodexRuntimeConfig | None = None
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)

class CostBudget(BaseModel):
    """Plan-level cost budget."""
    total_usd: float = 25.0
    on_exceed: Literal['pause', 'cancel', 'warn'] = 'pause'

class CircuitBreaker(BaseModel):
    """Circuit breaker settings."""
    threshold: int = 3
    action: Literal['cancel_all', 'pause', 'notify_only'] = 'cancel_all'

class DependencyContext(BaseModel):
    """Dependency context inheritance settings."""
    mode: Literal['full', 'diff_only', 'paths'] = 'full'
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)

class WorktreeSetup(BaseModel):
    """Command to prepare each agent worktree before runtime launch."""
    command: str = Field(min_length=1)
    timeout_seconds: int | None = Field(default=None, gt=0)
    env: dict[str, str] = Field(default_factory=dict)
    cache: bool = False
    cache_paths: list[str] = Field(
        default_factory=lambda: [
            'pnpm-lock.yaml',
            'package-lock.json',
            'yarn.lock',
            'bun.lock',
            'uv.lock',
            'poetry.lock',
            'requirements.txt',
        ]
    )

class WorktreeSource(BaseModel):
    """Source revision for new agent worktrees."""
    base_ref: str | None = None
    fetch: bool = False
    env_refs: dict[str, str] = Field(default_factory=dict)

class TelemetryConfig(BaseModel):
    """External telemetry and local trace mirror settings."""
    enabled: bool = False
    exporter: Literal['none', 'otlp'] = 'none'
    capture: Literal['orchestrator', 'full'] = 'full'
    failure_policy: Literal['degrade', 'fail'] = 'degrade'

class ArtifactConfig(BaseModel):
    """Durable artifact capture settings for deployed runs."""
    capture_raw: bool = False

class GitDelivery(BaseModel):
    """Git delivery behavior for completed agent work."""
    commit: bool = True
    push: bool = False
    remote: str = 'origin'
    push_timeout_seconds: int | None = Field(default=60, gt=0)
    env_refs: dict[str, str] = Field(default_factory=dict)

class CommandPolicy(BaseModel):
    """Policy for plan-provided shell commands."""
    mode: Literal['allowlist', 'unrestricted'] = 'allowlist'
    allowed_commands: list[str] = Field(
        default_factory=lambda: [
            'true',
            'pytest',
            'python',
            'python3',
            'uv',
            'ruff',
            'mypy',
            'pyright',
            'pnpm',
            'npm',
            'bun',
            'yarn',
            'make',
            'git',
        ]
    )

class CleanupPolicy(BaseModel):
    """Worker-local scratch cleanup behavior after terminal agent execution."""
    worktree: bool = False

class RuntimeIsolation(BaseModel):
    """Required worker isolation boundary for write-capable runtime execution."""
    accepted: list[Literal['container', 'jail', 'vm']] = Field(default_factory=lambda: ['container', 'jail', 'vm'])

class Orchestration(BaseModel):
    """Orchestration settings."""
    event_injection: bool = True
    circuit_breaker: CircuitBreaker | None = None
    dependency_context: DependencyContext | None = None
    worktree_source: WorktreeSource | None = None
    worktree_setup: WorktreeSetup | None = None
    telemetry: TelemetryConfig | None = None
    artifacts: ArtifactConfig | None = None
    git: GitDelivery | None = None
    command_policy: CommandPolicy | None = None
    cleanup: CleanupPolicy | None = None
    runtime_isolation: RuntimeIsolation | None = None
    concurrency_limit: int | None = Field(default=None, gt=0)
    stuck_threshold: int | None = None

class Milestone(BaseModel):
    """Named milestone for progress tracking."""
    name: str
    description: str = ''

class ManagerSettings(BaseModel):
    """Manager-specific settings."""
    max_subagents: int = 5
    event_poll_interval: int = 10
    guidance_enabled: bool = True

class AgentSpec(BaseModel):
    """Agent specification in a plan."""
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    type: Literal['worker', 'manager'] = 'worker'
    use_role: str | None = None
    prompt: str
    max_iterations: int | None = None
    runtime_timeout_seconds: int | None = Field(default=None, gt=0)
    check_timeout_seconds: int | None = Field(default=None, gt=0)
    check: str | None = None
    on_failure: Literal['continue', 'stop', 'retry'] | None = None
    retry_count: int | None = None
    model: str | None = None
    max_cost_usd: float | None = None
    runtime: RuntimeName | None = None
    codex: CodexRuntimeConfig | None = None
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    write_allowed: bool | None = None
    depends_on: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_refs: dict[str, str] = Field(default_factory=dict)
    milestones: list[Milestone] = Field(default_factory=list)
    manager: ManagerSettings | None = None

class PlanSpec(BaseModel):
    """Full plan specification."""
    name: str
    description: str = ''
    run: RunConfig | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    cost_budget: CostBudget | None = None
    shared_context: list[str] = Field(default_factory=list)
    orchestration: Orchestration | None = None
    agents: list[AgentSpec] = Field(default_factory=list)
    on_complete: Literal['none', 'notify'] = 'none'
