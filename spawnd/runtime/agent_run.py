"""Runtime configuration for a single agent execution."""
from dataclasses import dataclass
from pathlib import Path

@dataclass
class AgentConfig:
    """Configuration for running an agent."""
    name: str
    run_id: str
    prompt: str
    worktree: Path
    check_command: str = 'true'
    model: str = 'sonnet'
    max_iterations: int = 30
    max_cost_usd: float = 5.0
    parent: str | None = None
    env: dict[str, str] | None = None
    shared_context: str = ''
    runtime: str = 'claude'

    def tree_path(self) -> str:
        """Get full hierarchy path."""
        if self.parent and (not self.name.startswith(f'{self.parent}.')):
            return f'{self.parent}.{self.name}'
        return self.name
