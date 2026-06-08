"""Runtime configuration for a single agent execution."""
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from spawnd.models.specs import CodexRuntimeConfig, McpServerSpec
from spawnd.runtime.observer import NullRuntimeObserver, RuntimeObserver

@dataclass
class AgentConfig:
    """Configuration for running an agent."""
    name: str
    run_id: str
    prompt: str
    worktree: Path
    check_command: str = 'true'
    model: str | None = 'sonnet'
    max_iterations: int = 30
    runtime_timeout_seconds: int | None = None
    check_timeout_seconds: int | None = 300
    max_cost_usd: float = 5.0
    parent: str | None = None
    env: dict[str, str] | None = None
    shared_context: str = ''
    runtime: str = 'claude'
    write_allowed: bool = True
    codex: CodexRuntimeConfig | None = None
    mcp_servers: list[McpServerSpec] | None = None
    resume_session_id: str | None = None
    resume_thread_id: str | None = None
    observer: RuntimeObserver = NullRuntimeObserver()

    def tree_path(self) -> str:
        """Get full hierarchy path."""
        if self.parent and (not self.name.startswith(f'{self.parent}.')):
            return f'{self.parent}.{self.name}'
        return self.name

    def execution_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build the environment passed to provider runtimes and their tools."""

        env = dict(base or {})
        if self.env:
            env.update(self.env)
        env.update(
            {
                'SPAWND_RUN_ID': self.run_id,
                'SPAWND_AGENT_NAME': self.name,
                'SPAWND_PARENT_AGENT': self.parent or '',
                'SPAWND_TREE_PATH': self.tree_path(),
            }
        )
        return env
