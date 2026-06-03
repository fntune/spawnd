"""Tests for the Codex executor."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from spawnd.runtime.executor import AgentConfig
from spawnd.runtime.executors.base import get_executor
from spawnd.runtime.executors.codex import CodexExecutor
from spawnd.storage.db import get_agent, init_db, insert_agent, insert_plan
from spawnd.tools.toolset import manager_toolset, worker_toolset

from tests.helpers import require_row


def _fake_codex(tmp_path):
    script = tmp_path / "codex"
    script.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$@" > "$PWD/codex-args.txt"
last_message=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    shift
    last_message="$1"
  fi
  shift || true
done
printf 'codex stdout\\n'
printf 'agent=%s tree=%s flag=%s\\n' "$SPAWND_AGENT_NAME" "$SPAWND_TREE_PATH" "${MY_FLAG:-}" > "$PWD/codex-env.txt"
if [ -n "$last_message" ]; then
  printf 'final from codex\\n' > "$last_message"
fi
"""
    )
    script.chmod(0o755)
    return script


def _insert_codex_agent(run_id: str, worktree, *, agent_type: str = "worker"):
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(
        db,
        run_id,
        "worker",
        "task",
        agent_type=agent_type,
        check_command="true",
        runtime="codex",
        cost_source="codex",
        env={"MY_FLAG": "set"},
    )
    db.execute(
        "UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?",
        (str(worktree), run_id, "worker"),
    )
    db.commit()
    db.close()


def _agent_config(run_id: str, worktree, *, env: dict[str, str] | None = None):
    return AgentConfig(
        name="worker",
        run_id=run_id,
        prompt="make a small improvement",
        worktree=worktree,
        check_command="printf check-ok > check.txt",
        model="sonnet",
        runtime="codex",
        env=env,
    )


def test_codex_executor_registers():
    ex = get_executor("codex")
    assert isinstance(ex, CodexExecutor)
    assert ex.runtime == "codex"


@pytest.mark.asyncio
async def test_codex_executor_runs_cli_exec_and_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake_codex = _fake_codex(tmp_path)

    run_id = "codex-run"
    _insert_codex_agent(run_id, worktree)

    result = await CodexExecutor().run(
        _agent_config(
            run_id,
            worktree,
            env={
                "SPAWND_CODEX_BIN": str(fake_codex),
                "SPAWND_CODEX_ENGINE": "cli",
                "SPAWND_CODEX_MODEL": "gpt-5",
                "MY_FLAG": "set",
            },
        ),
        worker_toolset(system_prompt="sys"),
    )

    assert result["success"] is True
    assert (worktree / "check.txt").read_text() == "check-ok"
    assert (worktree / "codex-env.txt").read_text() == "agent=worker tree=worker flag=set\n"

    args = (worktree / "codex-args.txt").read_text().splitlines()
    assert args[:2] == ["exec", "--cd"]
    assert str(worktree) in args
    assert "--model" in args
    assert args[args.index("--model") + 1] == "gpt-5"
    assert "--output-last-message" in args
    assert "--ephemeral" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[-1] == "make a small improvement"

    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, "worker"))
    db.close()
    assert agent["status"] == "completed"
    assert agent["iteration"] == 1

    log = (tmp_path / ".spawnd" / "runs" / run_id / "logs" / "worker.log").read_text()
    assert "codex stdout" in log
    assert "final from codex" in log


@pytest.mark.asyncio
async def test_codex_executor_runs_sdk_with_context_shutdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_id = "codex-sdk-run"
    _insert_codex_agent(run_id, worktree)

    calls: list[tuple[str, object]] = []

    class FakeSandbox:
        read_only = "read-only"
        workspace_write = "workspace-write"
        full_access = "full-access"

    class FakeApprovalMode:
        deny_all = "deny_all"
        auto_review = "auto_review"

    class FakeThread:
        id = "thread-1"

        async def run(self, prompt, **kwargs):
            calls.append(("run", {"prompt": prompt, **kwargs}))
            (worktree / "sdk-ran.txt").write_text("yes")
            return SimpleNamespace(
                id="turn-1",
                status=SimpleNamespace(value="completed"),
                final_response="sdk final",
                usage=None,
            )

    class FakeAsyncCodex:
        async def __aenter__(self):
            calls.append(("enter", None))
            return self

        async def __aexit__(self, exc_type, exc, tb):
            calls.append(("exit", exc_type))

        async def thread_start(self, **kwargs):
            calls.append(("thread_start", kwargs))
            return FakeThread()

    fake_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(
        "spawnd.runtime.executors.codex._load_codex_sdk",
        lambda: fake_module,
    )

    result = await CodexExecutor().run(
        _agent_config(
            run_id,
            worktree,
            env={"SPAWND_CODEX_ENGINE": "sdk", "SPAWND_CODEX_MODEL": "gpt-5"},
        ),
        worker_toolset(system_prompt="sys"),
    )

    assert result["success"] is True
    assert result["vendor_session_id"] == "thread-1"
    assert (worktree / "sdk-ran.txt").read_text() == "yes"
    assert calls[0] == ("enter", None)
    assert calls[-1] == ("exit", None)
    assert calls[1] == (
        "thread_start",
        {
            "approval_mode": "deny_all",
            "cwd": str(worktree),
            "ephemeral": True,
            "model": "gpt-5",
            "sandbox": "workspace-write",
        },
    )
    assert calls[2][0] == "run"

    log = (tmp_path / ".spawnd" / "runs" / run_id / "logs" / "worker.log").read_text()
    assert "[codex sdk]" in log
    assert "thread: thread-1" in log
    assert "sdk final" in log


@pytest.mark.asyncio
async def test_codex_executor_fails_when_forced_sdk_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_id = "codex-sdk-missing"
    _insert_codex_agent(run_id, worktree)
    monkeypatch.setattr("spawnd.runtime.executors.codex._load_codex_sdk", lambda: None)

    result = await CodexExecutor().run(
        _agent_config(run_id, worktree, env={"SPAWND_CODEX_ENGINE": "sdk"}),
        worker_toolset(system_prompt="sys"),
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "openai_codex is not installed" in result["error"]


@pytest.mark.asyncio
async def test_codex_executor_fails_when_check_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake_codex = _fake_codex(tmp_path)

    run_id = "codex-check-fail"
    _insert_codex_agent(run_id, worktree)

    config = _agent_config(
        run_id,
        worktree,
        env={"SPAWND_CODEX_BIN": str(fake_codex), "SPAWND_CODEX_ENGINE": "cli"},
    )
    config.check_command = "printf check-failed >&2; exit 7"
    config.model = "gpt-5"
    result = await CodexExecutor().run(config, worker_toolset(system_prompt="sys"))

    assert result["success"] is False
    assert result["status"] == "failed"
    assert "check-failed" in result["error"]

    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, "worker"))
    db.close()
    assert agent["status"] == "failed"
    assert "check-failed" in agent["error"]


@pytest.mark.asyncio
async def test_codex_executor_rejects_manager_toolset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    run_id = "codex-manager"
    _insert_codex_agent(run_id, worktree, agent_type="manager")
    config = _agent_config(run_id, worktree)
    config.prompt = "coordinate work"
    config.check_command = "true"
    config.model = "gpt-5"
    result = await CodexExecutor().run(config, manager_toolset(system_prompt="sys"))

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["error"] == "codex runtime currently supports worker agents only"

    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, "worker"))
    db.close()
    assert agent["status"] == "failed"
    assert agent["error"] == "codex runtime currently supports worker agents only"
