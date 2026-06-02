"""Tests for the public Python API (spawnd.api)."""

import pytest

from spawnd import AgentSpec, PlanSpec, agent, handoff, pipeline, run
from spawnd.storage.db import get_agents, get_db, get_plan


@pytest.fixture
def spawnd_env(tmp_path, monkeypatch):
    """Point spawnd at a fresh tmp directory and stub worktree creation."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spawnd" / "runs").mkdir(parents=True)

    def fake_create_worktree(run_id, agent_name, *args, **kwargs):
        path = tmp_path / ".spawnd" / "runs" / run_id / "worktrees" / agent_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr("spawnd.runtime.scheduler.create_worktree", fake_create_worktree)
    monkeypatch.setattr(
        "spawnd.runtime.scheduler.setup_worktree_with_deps", lambda *a, **kw: None
    )
    return tmp_path


def test_agent_builder_omits_none_fields():
    """Passing None for optional kwargs should let pydantic defaults apply."""
    spec = agent("a", "do X")
    assert spec.name == "a"
    assert spec.prompt == "do X"
    assert spec.type == "worker"
    assert spec.depends_on == []
    assert spec.env == {}
    assert spec.model is None  # None means "fall through to plan defaults"


def test_agent_builder_passes_through_fields():
    spec = agent(
        "svc",
        "build service",
        depends_on=["db"],
        check="pytest",
        model="opus",
        type="manager",
        use_role="architect",
        max_iterations=10,
        max_cost_usd=2.5,
        on_failure="retry",
        retry_count=2,
        env={"FOO": "bar"},
    )
    assert spec.depends_on == ["db"]
    assert spec.check == "pytest"
    assert spec.model == "opus"
    assert spec.type == "manager"
    assert spec.use_role == "architect"
    assert spec.env == {"FOO": "bar"}


@pytest.mark.asyncio
async def test_run_single_agent_completes(spawnd_env):
    result = await run(
        [agent("solo", "do the thing", check="true")],
        name="api-single",
        use_mock=True,
    )

    assert result.success is True
    assert result.completed == ["solo"]
    assert result.failed == []

    with get_db(result.run_id) as db:
        plan = get_plan(db, result.run_id)
        assert plan is not None
        assert plan["status"] == "completed"

        agents = get_agents(db, result.run_id)
        assert {a["name"]: a["status"] for a in agents} == {"solo": "completed"}


@pytest.mark.asyncio
async def test_run_with_explicit_run_id(spawnd_env):
    result = await run(
        [agent("a", "task", check="true")],
        run_id="explicit-run-id-001",
        use_mock=True,
    )
    assert result.run_id == "explicit-run-id-001"
    assert (spawnd_env / ".spawnd" / "runs" / "explicit-run-id-001" / "spawnd.db").exists()


@pytest.mark.asyncio
async def test_api_accepts_codex_runtime_on_agentspec(spawnd_env):
    result = await run(
        [agent("codex", "task", check="true", runtime="codex")],
        name="codex-runtime",
        use_mock=True,
    )

    assert result.success is True
    with get_db(result.run_id) as db:
        row = db.execute(
            "SELECT runtime, cost_source FROM agents WHERE run_id = ? AND name = ?",
            (result.run_id, "codex"),
        ).fetchone()
    assert row["runtime"] == "codex"
    assert row["cost_source"] == "codex-cli"


@pytest.mark.asyncio
async def test_run_with_dependency_runs_both(spawnd_env):
    result = await run(
        [
            agent("first", "step 1", check="true"),
            agent("second", "step 2", check="true", depends_on=["first"]),
        ],
        name="api-deps",
        use_mock=True,
    )

    assert result.success is True
    assert set(result.completed) == {"first", "second"}


@pytest.mark.asyncio
async def test_plan_spec_input_is_accepted(spawnd_env):
    """run() should accept a PlanSpec directly, not just a list of agents."""
    plan = PlanSpec(
        name="direct-plan",
        agents=[AgentSpec(name="x", prompt="task", check="true")],
    )
    result = await run(plan, use_mock=True)
    assert result.success is True
    assert result.completed == ["x"]


@pytest.mark.asyncio
async def test_invalid_plan_raises_value_error(spawnd_env):
    """Circular dependencies must be rejected before the scheduler runs."""
    with pytest.raises(ValueError, match="Invalid plan"):
        await run(
            [
                agent("a", "x", depends_on=["b"]),
                agent("b", "y", depends_on=["a"]),
            ],
            use_mock=True,
        )


@pytest.mark.asyncio
async def test_run_resume_requires_run_id(spawnd_env):
    with pytest.raises(ValueError, match="resume=True requires run_id"):
        await run([agent("a", "x")], resume=True, use_mock=True)


@pytest.mark.asyncio
async def test_run_resume_rejects_stale_run_db(spawnd_env):
    stale = spawnd_env / ".spawnd" / "runs" / "stale-run"
    stale.mkdir(parents=True)
    (stale / "spawnd.db").write_text("")

    with pytest.raises(ValueError, match="Run not found: stale-run"):
        await run([agent("a", "x")], run_id="stale-run", resume=True, use_mock=True)


@pytest.mark.asyncio
async def test_unknown_dependency_raises(spawnd_env):
    with pytest.raises(ValueError, match="Invalid plan"):
        await run(
            [agent("a", "x", depends_on=["nonexistent"])],
            use_mock=True,
        )


@pytest.mark.asyncio
async def test_pipeline_auto_chains_depends_on(spawnd_env):
    """pipeline() should thread depends_on by list order without mutating inputs."""
    a = agent("a", "x", check="true")
    b = agent("b", "y", check="true")
    c = agent("c", "z", check="true")

    result = await pipeline([a, b, c], name="api-pipeline", use_mock=True)

    assert result.success is True
    assert set(result.completed) == {"a", "b", "c"}

    # Input specs must be untouched — pipeline copies, doesn't mutate.
    assert a.depends_on == []
    assert b.depends_on == []
    assert c.depends_on == []


@pytest.mark.asyncio
async def test_pipeline_preserves_existing_depends_on(spawnd_env):
    """A step that already depends on something else keeps that edge."""
    independent = agent("shared", "x", check="true")
    a = agent("a", "y", check="true")
    b = agent("b", "z", check="true", depends_on=["shared"])

    result = await pipeline([independent, a, b], name="api-pipeline-mixed", use_mock=True)
    assert result.success is True
    assert set(result.completed) == {"shared", "a", "b"}


@pytest.mark.asyncio
async def test_handoff_runs_both(spawnd_env):
    result = await handoff(
        agent("a", "first", check="true"),
        agent("b", "second", check="true"),
        use_mock=True,
    )
    assert result.success is True
    assert set(result.completed) == {"a", "b"}
