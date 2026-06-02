"""Runtime state models for spawnd.dev."""
from typing import Literal
from pydantic import BaseModel, Field

class AgentState(BaseModel):
    """Runtime state of an agent."""
    name: str
    run_id: str
    status: Literal['pending', 'running', 'blocked', 'checking', 'paused', 'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'] = 'pending'
    iteration: int = 0
    max_iterations: int = 30
    worktree: str | None = None
    branch: str | None = None
    prompt: str = ''
    check_command: str | None = None
    model: str = 'sonnet'
    parent: str | None = None
    cost_usd: float = 0.0
    max_cost_usd: float = 5.0
    error: str | None = None
    depends_on: list[str] = Field(default_factory=list)

class Event(BaseModel):
    """Event emitted by an agent."""
    id: str
    run_id: str
    ts: str
    agent: str
    event_type: Literal['started', 'progress', 'clarification', 'blocker', 'done', 'error', 'cascade_skip', 'circuit_breaker_tripped']
    data: dict = Field(default_factory=dict)

class Response(BaseModel):
    """Response to a clarification request."""
    id: int
    run_id: str
    clarification_id: str
    response: str
    consumed: bool = False
