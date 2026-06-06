"""Input specification models for spawnd.dev."""
from typing import Literal
from pydantic import BaseModel, Field
AGENT_NAME_PATTERN = '^[A-Za-z0-9_.-]+$'
RuntimeName = Literal['claude', 'openai', 'codex']

class RunConfig(BaseModel):
    """Run identity and resumption settings."""
    id: str | None = None
    resume: bool = False

class Defaults(BaseModel):
    """Plan-level default settings."""
    max_iterations: int = 30
    check: str | None = 'true'
    on_failure: Literal['continue', 'stop', 'retry'] = 'continue'
    retry_count: int = 3
    model: str = 'sonnet'
    max_cost_usd: float = 5.0
    runtime: RuntimeName = 'claude'

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

class WorktreeSource(BaseModel):
    """Source revision for new agent worktrees."""
    base_ref: str | None = None
    fetch: bool = False

class TelemetryConfig(BaseModel):
    """External telemetry and local trace mirror settings."""
    enabled: bool = False
    exporter: Literal['none', 'otlp'] = 'none'
    capture: Literal['orchestrator', 'full'] = 'full'
    failure_policy: Literal['degrade', 'fail'] = 'degrade'

class ArtifactConfig(BaseModel):
    """Durable artifact capture settings for deployed runs."""
    capture_raw: bool = False

class Orchestration(BaseModel):
    """Orchestration settings."""
    event_injection: bool = True
    circuit_breaker: CircuitBreaker | None = None
    dependency_context: DependencyContext | None = None
    worktree_source: WorktreeSource | None = None
    worktree_setup: WorktreeSetup | None = None
    telemetry: TelemetryConfig | None = None
    artifacts: ArtifactConfig | None = None
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
    check: str | None = None
    on_failure: Literal['continue', 'stop', 'retry'] | None = None
    retry_count: int | None = None
    model: str | None = None
    max_cost_usd: float | None = None
    runtime: RuntimeName | None = None
    depends_on: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
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
