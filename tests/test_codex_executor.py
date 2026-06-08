"""Tests for Codex runtime execution boundaries."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spawnd.models.specs import CodexRuntimeConfig, McpServerSpec
from spawnd.runtime.agent_run import AgentConfig
from spawnd.runtime.executors.codex import CodexExecutor, _apply_codex_config, _apply_codex_mcp_env, _build_codex_command, _extract_cli_facts, _sdk_start_or_resume_thread
from spawnd.tools.toolset import manager_toolset, worker_toolset


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
        timeout_seconds: int | None,
    ) -> subprocess.CompletedProcess:
        _ = (cwd, env, timeout_seconds)
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


def test_agent_codex_config_overrides_ambient_env(tmp_path: Path) -> None:
    config = AgentConfig(
        name="agent",
        run_id="run-1",
        prompt="task",
        worktree=tmp_path,
        runtime="codex",
        codex=CodexRuntimeConfig(
            engine="cli",
            sandbox="read-only",
            approval_mode="auto_review",
            ephemeral=False,
        ),
    )

    env = _apply_codex_config(
        config,
        {
            "SPAWND_CODEX_ENGINE": "sdk",
            "SPAWND_CODEX_SANDBOX": "danger-full-access",
            "SPAWND_CODEX_APPROVAL_MODE": "deny_all",
            "SPAWND_CODEX_EPHEMERAL": "true",
            "SPAWND_CODEX_BIN": "codex",
        },
    )
    command = _build_codex_command(config, env, tmp_path / "last.txt")

    assert env["SPAWND_CODEX_ENGINE"] == "cli"
    assert env["SPAWND_CODEX_APPROVAL_MODE"] == "auto_review"
    assert "--ephemeral" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--json" in command


def test_codex_cli_resume_command_uses_stored_thread_id(tmp_path: Path) -> None:
    config = AgentConfig(
        name="agent",
        run_id="run-1",
        prompt="continue task",
        worktree=tmp_path,
        runtime="codex",
        resume_thread_id="session-1",
    )

    command = _build_codex_command(config, {"SPAWND_CODEX_BIN": "codex"}, tmp_path / "last.txt")

    assert command[:3] == ["codex", "exec", "resume"]
    assert "--cd" not in command
    assert "--sandbox" not in command
    assert "--json" in command
    assert command[-2:] == ["session-1", "continue task"]


def test_codex_cli_command_injects_spawnd_and_external_mcp_config(tmp_path: Path) -> None:
    config = AgentConfig(
        name="manager",
        run_id="run-1",
        prompt="manage",
        worktree=tmp_path,
        runtime="codex",
        env={"SPAWND_AGENT_TYPE": "manager"},
        mcp_servers=[
            McpServerSpec(name="docs", type="http", url="https://mcp.example.test", header_refs={"Authorization": "SPAWND_MCP_TOKEN"}),
            McpServerSpec(name="fs", type="stdio", command="fs-mcp", args=["--root", "."], env_refs={"FS_TOKEN": "SPAWND_FS_TOKEN"}),
        ],
    )
    env = config.execution_env({"SPAWND_CODEX_BIN": "codex", "SPAWND_MCP_TOKEN": "token", "SPAWND_FS_TOKEN": "fs-token"})
    env = _apply_codex_mcp_env(config, env)

    command = _build_codex_command(config, env, tmp_path / "last.txt")

    assert "mcp_servers.spawnd.command=\"python\"" in command
    assert 'mcp_servers.spawnd.args=["-m","spawnd.tools.mcp"]' in command
    assert 'mcp_servers.spawnd.env.SPAWND_AGENT_TYPE="manager"' in command
    assert 'mcp_servers.docs.url="https://mcp.example.test"' in command
    assert 'mcp_servers.docs.bearer_token_env_var="SPAWND_MCP_TOKEN"' in command
    assert 'mcp_servers.fs.command="fs-mcp"' in command
    assert 'mcp_servers.fs.args=["--root","."]' in command
    assert env["FS_TOKEN"] == "fs-token"


def test_codex_cli_jsonl_usage_and_thread_facts_are_extracted():
    stdout = "\n".join(
        [
            '{"type":"session","session_id":"session-1","model":"gpt-5"}',
            '{"type":"usage","usage":{"inputTokens":100,"outputTokens":25,"reasoningOutputTokens":5}}',
            'not-json',
            '{"type":"usage","usage":{"prompt_tokens":10,"completion_tokens":3}}',
        ]
    )

    assert _extract_cli_facts(stdout) == {
        "thread_id": "session-1",
        "model": "gpt-5",
        "input_tokens": 110,
        "output_tokens": 33,
    }


@pytest.mark.asyncio
async def test_codex_sdk_resumes_existing_thread(tmp_path: Path) -> None:
    class FakeCodex:
        async def thread_start(self, **kwargs):
            _ = kwargs
            raise AssertionError("should resume instead of starting")

        async def thread_resume(self, thread_id, **kwargs):
            return {"thread_id": thread_id, "kwargs": kwargs}

    config = AgentConfig(
        name="agent",
        run_id="run-1",
        prompt="continue",
        worktree=tmp_path,
        runtime="codex",
        resume_thread_id="thread-1",
    )

    thread = await _sdk_start_or_resume_thread(FakeCodex(), config, {"cwd": str(tmp_path)})

    assert thread == {"thread_id": "thread-1", "kwargs": {"cwd": str(tmp_path)}}


@pytest.mark.asyncio
async def test_codex_sdk_resume_fails_when_sdk_has_no_resume(tmp_path: Path) -> None:
    class FakeCodex:
        async def thread_start(self, **kwargs):
            return {"new": kwargs}

    config = AgentConfig(
        name="agent",
        run_id="run-1",
        prompt="continue",
        worktree=tmp_path,
        runtime="codex",
        resume_thread_id="thread-1",
    )

    with pytest.raises(RuntimeError, match="exposes no thread_resume/resume_thread"):
        await _sdk_start_or_resume_thread(FakeCodex(), config, {"cwd": str(tmp_path)})


@pytest.mark.asyncio
async def test_codex_run_estimates_cost_from_exposed_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_sdk(self, config, env, sdk_module):
        _ = (self, config, env, sdk_module)
        return {
            "status": "completed",
            "final_message": "done",
            "thread_id": "thread-1",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "model": "gpt-5",
        }

    monkeypatch.setattr("spawnd.runtime.executors.codex._load_codex_sdk", lambda: object())
    monkeypatch.setattr(CodexExecutor, "_run_sdk", fake_run_sdk)

    result = await CodexExecutor().run(
        AgentConfig(
            name="agent",
            run_id="run-1",
            prompt="task",
            worktree=tmp_path,
            runtime="codex",
            max_cost_usd=1.0,
        ),
        worker_toolset(),
    )

    assert result["status"] == "completed"
    assert result["cost"] > 0
    assert result["cost_source"] == "estimated"


@pytest.mark.asyncio
async def test_codex_run_enforces_estimated_cost_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_sdk(self, config, env, sdk_module):
        _ = (self, config, env, sdk_module)
        return {
            "status": "completed",
            "final_message": "done",
            "thread_id": "thread-1",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "model": "gpt-5",
        }

    monkeypatch.setattr("spawnd.runtime.executors.codex._load_codex_sdk", lambda: object())
    monkeypatch.setattr(CodexExecutor, "_run_sdk", fake_run_sdk)

    result = await CodexExecutor().run(
        AgentConfig(
            name="agent",
            run_id="run-1",
            prompt="task",
            worktree=tmp_path,
            runtime="codex",
            max_cost_usd=0.01,
        ),
        worker_toolset(),
    )

    assert result["status"] == "cost_exceeded"
    assert result["cost_source"] == "estimated"


@pytest.mark.asyncio
async def test_codex_manager_forces_cli_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_run_cli(self, config, env):
        _ = self
        captured["env"] = env
        return {"status": "completed", "final_message": "done", "thread_id": "session-1"}

    monkeypatch.setattr("spawnd.runtime.executors.codex._load_codex_sdk", lambda: object())
    monkeypatch.setattr(CodexExecutor, "_run_cli", fake_run_cli)

    result = await CodexExecutor().run(
        AgentConfig(
            name="manager",
            run_id="run-1",
            prompt="manage",
            worktree=tmp_path,
            runtime="codex",
            env={"SPAWND_AGENT_TYPE": "manager"},
        ),
        manager_toolset(),
    )

    assert result["status"] == "completed"
    assert result["vendor_session_id"] == "session-1"
    assert captured["env"]["SPAWND_AGENT_TYPE"] == "manager"
