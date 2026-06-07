"""Tests for runtime agent execution configuration."""
from pathlib import Path

from spawnd.runtime.agent_run import AgentConfig


def test_execution_env_preserves_spawnd_context_over_agent_env(tmp_path: Path) -> None:
    config = AgentConfig(
        name='worker',
        run_id='run-1',
        prompt='task',
        worktree=tmp_path,
        parent='manager',
        env={
            'CUSTOM': 'value',
            'SPAWND_RUN_ID': 'spoofed-run',
            'SPAWND_AGENT_NAME': 'spoofed-agent',
            'SPAWND_PARENT_AGENT': 'spoofed-parent',
            'SPAWND_TREE_PATH': 'spoofed.path',
        },
    )

    env = config.execution_env({'PATH': '/bin', 'SPAWND_RUN_ID': 'ambient-run'})

    assert env['PATH'] == '/bin'
    assert env['CUSTOM'] == 'value'
    assert env['SPAWND_RUN_ID'] == 'run-1'
    assert env['SPAWND_AGENT_NAME'] == 'worker'
    assert env['SPAWND_PARENT_AGENT'] == 'manager'
    assert env['SPAWND_TREE_PATH'] == 'manager.worker'
