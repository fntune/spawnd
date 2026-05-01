"""Tests for the OpenAI executor, its tool bridges, and the cost estimator.

Skipped when the openai-agents SDK isn't installed (optional [openai] extra).
"""

from __future__ import annotations

import asyncio
import json
import pytest

pytest.importorskip("agents", reason="openai-agents SDK not installed")

from types import SimpleNamespace

from spawnd.core.budget import estimate_cost_usd
from spawnd.runtime.executors.base import get_executor
from spawnd.runtime.executors.openai import OpenAIExecutor
from spawnd.runtime.executor import AgentConfig
from spawnd.storage.db import get_agent, init_db, insert_agent, insert_plan
from spawnd.tools.openai_code import build_code_tools
from spawnd.tools.toolset import worker_toolset


def test_openai_executor_registers():
    ex = get_executor("openai")
    assert isinstance(ex, OpenAIExecutor)
    assert ex.runtime == "openai"


def test_budget_estimates_known_model():
    cost = estimate_cost_usd("gpt-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(5.00 + 15.00, rel=1e-6)


def test_budget_estimates_unknown_model_uses_default():
    cost = estimate_cost_usd("no-such-model", 100_000, 100_000)
    expected = (100_000 / 1_000_000) * 5.00 + (100_000 / 1_000_000) * 15.00
    assert cost == pytest.approx(expected, rel=1e-6)


def test_budget_strips_date_suffix():
    assert estimate_cost_usd("gpt-5-2025-08-14", 1_000_000, 0) == pytest.approx(5.00)
    assert estimate_cost_usd("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.50)


def test_code_tools_write_allowed_includes_write_edit_bash(tmp_path):
    tools = build_code_tools(tmp_path, write_allowed=True)
    names = {t.name for t in tools}
    assert names == {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}


def test_code_tools_read_only_strips_write_edit_bash(tmp_path):
    tools = build_code_tools(tmp_path, write_allowed=False)
    names = {t.name for t in tools}
    assert names == {"Read", "Glob", "Grep"}


def test_code_tools_bash_receives_explicit_env(tmp_path):
    tools = build_code_tools(tmp_path, env={"MY_FLAG": "set"})
    bash = next(t for t in tools if t.name == "Bash")

    output = asyncio.run(
        bash.on_invoke_tool(
            None,
            json.dumps({"command": "printf ${MY_FLAG-unset}", "timeout_seconds": 1}),
        )
    )

    assert output == "set"


def test_factory_openai_builds_coord_tools():
    from spawnd.tools.factory_openai import build_manager_coord_tools, build_worker_coord_tools

    worker_tools = build_worker_coord_tools("run-x", "agent-a")
    worker_names = {t.name for t in worker_tools}
    assert worker_names == {
        "mark_complete", "request_clarification", "report_progress", "report_blocker",
    }

    manager_tools = build_manager_coord_tools("run-x", "manager-a")
    manager_names = {t.name for t in manager_tools}
    assert manager_names == {
        "spawn_worker", "respond_to_clarification", "cancel_worker",
        "get_worker_status", "get_pending_clarifications", "mark_plan_complete",
    }


@pytest.mark.asyncio
async def test_api_accepts_openai_runtime_on_agentspec(tmp_path, monkeypatch):
    """AgentSpec(runtime='openai') is accepted and persisted to the agents table."""
    from spawnd import agent, run
    from spawnd.storage.db import get_db

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)

    def fake_create_worktree(run_id, agent_name, *args, **kwargs):
        path = tmp_path / ".spawnd" / "runs" / run_id / "worktrees" / agent_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr("spawnd.runtime.scheduler.create_worktree", fake_create_worktree)
    monkeypatch.setattr("spawnd.runtime.scheduler.setup_worktree_with_deps", lambda *a, **kw: None)

    # use_mock=True bypasses the vendor executor — we're only verifying that
    # runtime="openai" survives the YAML/AgentSpec → scheduler → DB path.
    result = await run(
        [agent("gpt", "task", check="true", runtime="openai")],
        name="mixed-runtime",
        use_mock=True,
    )

    assert result.success is True
    with get_db(result.run_id) as db:
        row = db.execute(
            "SELECT runtime, cost_source FROM agents WHERE run_id = ? AND name = ?",
            (result.run_id, "gpt"),
        ).fetchone()
    assert row["runtime"] == "openai"
    assert row["cost_source"] == "estimated"


@pytest.mark.asyncio
async def test_openai_executor_passes_agent_env_to_bash(tmp_path, monkeypatch):
    """OpenAI runtime Bash tool should see the same agent env as Claude runtime."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)

    run_id = "openai-env"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(
        db,
        run_id,
        "worker",
        "task",
        check_command="true",
        runtime="openai",
        cost_source="estimated",
        env={"MY_FLAG": "set"},
    )
    worktree = tmp_path / ".spawnd" / "runs" / run_id / "worktrees" / "worker"
    worktree.mkdir(parents=True)
    db.execute(
        "UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?",
        (str(worktree), run_id, "worker"),
    )
    db.commit()
    db.close()

    class FakeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    captured: dict[str, str] = {}

    async def fake_run(agent, prompt, max_turns):
        bash = next(t for t in agent.tools if t.name == "Bash")
        output = await bash.on_invoke_tool(
            None,
            json.dumps({"command": "printf ${MY_FLAG-unset}", "timeout_seconds": 1}),
        )
        captured["bash_output"] = output
        mark_complete = next(t for t in agent.tools if t.name == "mark_complete")
        await mark_complete.on_invoke_tool(None, json.dumps({"summary": "done"}))
        return SimpleNamespace(raw_responses=[], final_output=output)

    monkeypatch.setattr("spawnd.runtime.executors.openai.Agent", FakeAgent)
    monkeypatch.setattr("spawnd.runtime.executors.openai.Runner.run", fake_run)

    executor = OpenAIExecutor()
    result = await executor.run(
        AgentConfig(
            name="worker",
            run_id=run_id,
            prompt="task",
            worktree=worktree,
            check_command="true",
            env={"MY_FLAG": "set"},
            runtime="openai",
        ),
        worker_toolset(system_prompt="sys"),
    )

    assert result["success"] is True
    assert captured["bash_output"] == "set"
    db = init_db(run_id)
    agent = get_agent(db, run_id, "worker")
    db.close()
    assert agent["status"] == "completed"
