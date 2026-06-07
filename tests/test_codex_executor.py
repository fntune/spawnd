"""Tests for Codex runtime execution boundaries."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spawnd.runtime.agent_run import AgentConfig
from spawnd.runtime.executors.codex import CodexExecutor


@pytest.mark.asyncio
async def test_codex_cli_last_message_scratch_stays_outside_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / ".spawnd-scratch" / "worktrees" / "run-1" / "agent"
    worktree.mkdir(parents=True)
    captured: dict[str, Path] = {}

    async def fake_run_subprocess(
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess:
        _ = (cwd, env)
        last_message_path = Path(args[args.index("--output-last-message") + 1])
        captured["last_message_path"] = last_message_path
        last_message_path.parent.mkdir(parents=True, exist_ok=True)
        last_message_path.write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("spawnd.runtime.executors.codex._run_subprocess", fake_run_subprocess)

    result = await CodexExecutor()._run_cli(
        AgentConfig(
            name="agent",
            run_id="run-1",
            prompt="task",
            worktree=worktree,
            runtime="codex",
        ),
        {"SPAWND_CODEX_BIN": "codex"},
    )

    assert result == {"status": "completed", "final_message": "done"}
    assert "last_message_path" in captured
    assert captured["last_message_path"].parent == tmp_path / ".spawnd-scratch" / "runtime" / "run-1" / "agent"
    assert not captured["last_message_path"].is_relative_to(worktree)
    assert not (worktree / ".spawnd-scratch").exists()
