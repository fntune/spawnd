"""Tests for scheduler module."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from spawnd.storage.db import (
    get_agent,
    get_agents,
    get_plan,
    init_db,
    insert_agent,
    insert_plan,
    open_db,
    update_agent_status,
    update_plan_status,
)
from spawnd.models.specs import AgentSpec, CostBudget, DependencyContext, Orchestration, CircuitBreaker, PlanSpec
from spawnd.runtime.scheduler import Scheduler, SchedulerResult


@pytest.fixture
def temp_spawnd_dir(tmp_path):
    """Create a temporary .spawnd runs directory."""
    runs_dir = tmp_path / ".spawnd" / "runs"
    runs_dir.mkdir(parents=True)
    return tmp_path


def create_test_plan(
    agents: list[AgentSpec],
    cost_budget: CostBudget | None = None,
    orchestration: Orchestration | None = None,
) -> PlanSpec:
    """Create a test plan spec."""
    return PlanSpec(
        name="test-plan",
        agents=agents,
        cost_budget=cost_budget,
        orchestration=orchestration,
    )


def test_scheduler_init(temp_spawnd_dir, monkeypatch):
    """Test scheduler initialization."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="test", prompt="Test task")]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-001", use_mock=True)

    assert scheduler.run_id == "test-run-001"
    assert scheduler.plan == plan
    assert scheduler.use_mock is True


def test_scheduler_init_db(temp_spawnd_dir, monkeypatch):
    """Test scheduler database initialization."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [
        AgentSpec(name="auth", prompt="Implement auth"),
        AgentSpec(name="cache", prompt="Implement cache", depends_on=["auth"]),
    ]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-002", use_mock=True)
    scheduler._init_db()

    # Verify DB was created
    db = scheduler.db
    assert db is not None

    # Verify plan was inserted
    plan_row = get_plan(db, "test-run-002")
    assert plan_row is not None
    assert plan_row["name"] == "test-plan"

    # Verify agents were inserted
    agent_rows = get_agents(db, "test-run-002")
    assert len(agent_rows) == 2
    names = {a["name"] for a in agent_rows}
    assert names == {"auth", "cache"}

    db.close()


def test_build_result_success(temp_spawnd_dir, monkeypatch):
    """Test _build_result with all agents completed."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-003", use_mock=True)
    scheduler._init_db()

    # Mark agent as completed
    update_agent_status(scheduler.db, "test-run-003", "a", "completed")

    result = scheduler._build_result()

    assert result.success is True
    assert result.completed == ["a"]
    assert result.failed == []

    # Verify plan status was updated
    plan_row = get_plan(scheduler.db, "test-run-003")
    assert plan_row["status"] == "completed"

    scheduler.db.close()


def test_build_result_failure(temp_spawnd_dir, monkeypatch):
    """Test _build_result with failed agent."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-004", use_mock=True)
    scheduler._init_db()

    # Mark agent as failed
    update_agent_status(scheduler.db, "test-run-004", "a", "failed", "Test error")

    result = scheduler._build_result()

    assert result.success is False
    assert result.completed == []
    assert result.failed == ["a"]

    # Verify plan status was updated
    plan_row = get_plan(scheduler.db, "test-run-004")
    assert plan_row["status"] == "failed"

    scheduler.db.close()


def test_check_failed_deps(temp_spawnd_dir, monkeypatch):
    """Test _check_failed_deps detection."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [
        AgentSpec(name="a", prompt="Task A"),
        AgentSpec(name="b", prompt="Task B", depends_on=["a"]),
    ]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-005", use_mock=True)
    scheduler._init_db()

    # Mark 'a' as failed
    update_agent_status(scheduler.db, "test-run-005", "a", "failed")

    # Check 'b' for failed deps
    agent_b = get_agent(scheduler.db, "test-run-005", "b")
    failed_deps = scheduler._check_failed_deps(agent_b)

    assert failed_deps == ["a"]

    scheduler.db.close()


def test_external_cancellation_detection(temp_spawnd_dir, monkeypatch):
    """Test that scheduler detects external cancellation."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-006", use_mock=True)
    scheduler._init_db()

    # Simulate external cancellation
    update_plan_status(scheduler.db, "test-run-006", "cancelled")

    # Check that plan shows cancelled
    plan_row = get_plan(scheduler.db, "test-run-006")
    assert plan_row["status"] == "cancelled"

    scheduler.db.close()


def test_resume_only_resets_retryable_agents(temp_spawnd_dir, monkeypatch):
    """Resume should not rerun agents that failed without retry policy."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [
        AgentSpec(name="retryable", prompt="Retry me", on_failure="retry", retry_count=2),
        AgentSpec(name="terminal", prompt="Do not rerun", on_failure="continue"),
    ]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-resume", use_mock=True)
    scheduler._init_db()
    update_agent_status(scheduler.db, "test-run-resume", "retryable", "failed", "boom")
    update_agent_status(scheduler.db, "test-run-resume", "terminal", "failed", "boom")
    scheduler.db.close()

    resumed = Scheduler(plan, run_id="test-run-resume", use_mock=True, resume=True)
    resumed._init_db()

    retryable = get_agent(resumed.db, "test-run-resume", "retryable")
    terminal = get_agent(resumed.db, "test-run-resume", "terminal")

    assert retryable["status"] == "pending"
    assert terminal["status"] == "failed"

    resumed.db.close()


def test_resume_requeues_paused_agents(temp_spawnd_dir, monkeypatch):
    """Manual resume should restart agents paused by cost/circuit-breaker logic."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="paused_worker", prompt="Resume me")]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-paused-resume", use_mock=True)
    scheduler._init_db()
    update_plan_status(scheduler.db, "test-run-paused-resume", "paused")
    update_agent_status(scheduler.db, "test-run-paused-resume", "paused_worker", "paused", "Paused by budget")
    scheduler.db.close()

    resumed = Scheduler(plan, run_id="test-run-paused-resume", use_mock=True, resume=True)
    resumed._init_db()

    paused_worker = get_agent(resumed.db, "test-run-paused-resume", "paused_worker")
    assert paused_worker["status"] == "pending"
    assert paused_worker["error"] is None

    resumed.db.close()


@pytest.mark.asyncio
async def test_spawn_agent_propagates_agentspec_env(temp_spawnd_dir, monkeypatch):
    """AgentSpec.env should survive persist-then-rehydrate into AgentConfig."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A", env={"MY_FLAG": "1", "TOKEN": "t"})]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-env", use_mock=True)
    scheduler._init_db()

    agent_row = get_agent(scheduler.db, "test-run-env", "a")
    captured: dict = {}

    monkeypatch.setattr("spawnd.runtime.scheduler.create_worktree", lambda *a, **kw: Path("/tmp/worktree-env"))
    monkeypatch.setattr("spawnd.runtime.scheduler.update_agent_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("spawnd.runtime.scheduler.load_shared_context", lambda *a, **kw: "")

    def fake_spawn_worker(config, use_mock=False):
        captured["env"] = config.env
        return "task"

    monkeypatch.setattr("spawnd.runtime.scheduler.spawn_worker", fake_spawn_worker)
    monkeypatch.setattr("spawnd.runtime.scheduler.setup_worktree_with_deps", lambda *a, **kw: None)

    await scheduler._spawn_agent(agent_row)

    assert captured["env"] == {"MY_FLAG": "1", "TOKEN": "t"}

    scheduler.db.close()


@pytest.mark.asyncio
async def test_scheduler_preserves_zero_valued_agent_overrides(temp_spawnd_dir, monkeypatch):
    """Explicit zero overrides should not fall through to plan defaults."""
    monkeypatch.chdir(temp_spawnd_dir)

    plan = PlanSpec(
        name="zero-overrides",
        agents=[
            AgentSpec(
                name="a",
                prompt="Task A",
                on_failure="retry",
                retry_count=0,
                max_cost_usd=0.0,
                max_iterations=0,
            )
        ],
    )

    scheduler = Scheduler(plan, run_id="test-run-zero-overrides", use_mock=True)
    scheduler._init_db()

    agent_row = get_agent(scheduler.db, "test-run-zero-overrides", "a")
    assert agent_row["retry_count"] == 0
    assert agent_row["max_cost_usd"] == 0.0
    assert agent_row["max_iterations"] == 0

    captured: dict = {}
    monkeypatch.setattr("spawnd.runtime.scheduler.create_worktree", lambda *a, **kw: Path("/tmp/worktree-zero"))
    monkeypatch.setattr("spawnd.runtime.scheduler.update_agent_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("spawnd.runtime.scheduler.load_shared_context", lambda *a, **kw: "")
    monkeypatch.setattr("spawnd.runtime.scheduler.setup_worktree_with_deps", lambda *a, **kw: None)

    def fake_spawn_worker(config, use_mock=False):
        captured["max_cost_usd"] = config.max_cost_usd
        captured["max_iterations"] = config.max_iterations
        return "task"

    monkeypatch.setattr("spawnd.runtime.scheduler.spawn_worker", fake_spawn_worker)

    await scheduler._spawn_agent(agent_row)

    assert captured["max_cost_usd"] == 0.0
    assert captured["max_iterations"] == 0

    update_agent_status(scheduler.db, "test-run-zero-overrides", "a", "failed", "boom")
    should_stop = await scheduler._handle_agent_failure("a", "boom")
    agent_row = get_agent(scheduler.db, "test-run-zero-overrides", "a")

    assert should_stop is False
    assert agent_row["status"] == "failed"
    assert agent_row["retry_attempt"] == 1

    scheduler.db.close()


@pytest.mark.asyncio
async def test_spawn_agent_passes_dependency_path_filters(temp_spawnd_dir, monkeypatch):
    """Dependency path filters should be passed into worktree setup."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [
        AgentSpec(name="base", prompt="Base task"),
        AgentSpec(name="child", prompt="Child task", depends_on=["base"]),
    ]
    orchestration = Orchestration(
        dependency_context=DependencyContext(
            mode="paths",
            include_paths=["src"],
            exclude_paths=["tests"],
        )
    )
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id="test-run-path-filters", use_mock=True)
    scheduler._init_db()

    agent_row = get_agent(scheduler.db, "test-run-path-filters", "child")
    called: dict = {}

    monkeypatch.setattr("spawnd.runtime.scheduler.create_worktree", lambda *args, **kwargs: Path("/tmp/child-worktree"))
    monkeypatch.setattr("spawnd.runtime.scheduler.update_agent_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr("spawnd.runtime.scheduler.load_shared_context", lambda *args, **kwargs: "")
    monkeypatch.setattr("spawnd.runtime.scheduler.spawn_worker", lambda *args, **kwargs: "task")

    def fake_setup(*args, **kwargs):
        called["include_paths"] = kwargs.get("include_paths")
        called["exclude_paths"] = kwargs.get("exclude_paths")

    monkeypatch.setattr("spawnd.runtime.scheduler.setup_worktree_with_deps", fake_setup)

    await scheduler._spawn_agent(agent_row)

    assert called["include_paths"] == ["src"]
    assert called["exclude_paths"] == ["tests"]

    scheduler.db.close()


@pytest.mark.asyncio
async def test_handle_cost_exceeded_warn(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with warn action."""
    monkeypatch.chdir(temp_spawnd_dir)

    cost_budget = CostBudget(total_usd=1.0, on_exceed="warn")
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, cost_budget=cost_budget)

    scheduler = Scheduler(plan, run_id="test-run-007", use_mock=True)
    scheduler._init_db()

    # Should return False (don't stop)
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is False

    # Plan should still be running
    plan_row = get_plan(scheduler.db, "test-run-007")
    assert plan_row["status"] == "running"

    scheduler.db.close()


@pytest.mark.asyncio
async def test_retry_count_allows_configured_number_of_retries(temp_spawnd_dir, monkeypatch):
    """retry_count=1 should allow one retry before exhausting."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A", on_failure="retry", retry_count=1)]
    plan = create_test_plan(agents)

    scheduler = Scheduler(plan, run_id="test-run-retry-count", use_mock=True)
    scheduler._init_db()
    update_agent_status(scheduler.db, "test-run-retry-count", "a", "failed", "boom")

    should_stop = await scheduler._handle_agent_failure("a", "boom")
    agent_row = get_agent(scheduler.db, "test-run-retry-count", "a")

    assert should_stop is False
    assert agent_row["status"] == "pending"
    assert agent_row["retry_attempt"] == 1

    scheduler.db.close()


@pytest.mark.asyncio
async def test_handle_cost_exceeded_cancel(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with cancel action."""
    monkeypatch.chdir(temp_spawnd_dir)

    cost_budget = CostBudget(total_usd=1.0, on_exceed="cancel")
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, cost_budget=cost_budget)

    scheduler = Scheduler(plan, run_id="test-run-008", use_mock=True)
    scheduler._init_db()

    # Should return True (stop)
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is True

    # Plan should be failed
    plan_row = get_plan(scheduler.db, "test-run-008")
    assert plan_row["status"] == "failed"

    scheduler.db.close()


@pytest.mark.asyncio
async def test_handle_cost_exceeded_pause(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with pause action (default)."""
    monkeypatch.chdir(temp_spawnd_dir)

    cost_budget = CostBudget(total_usd=1.0, on_exceed="pause")
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, cost_budget=cost_budget)

    scheduler = Scheduler(plan, run_id="test-run-009", use_mock=True)
    scheduler._init_db()

    # Should return True (stop)
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is True

    # Plan should be paused
    plan_row = get_plan(scheduler.db, "test-run-009")
    assert plan_row["status"] == "paused"

    scheduler.db.close()


def test_check_circuit_breaker_cancel_all(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with cancel_all action."""
    monkeypatch.chdir(temp_spawnd_dir)

    cb = CircuitBreaker(threshold=2, action="cancel_all")
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, orchestration=orchestration)

    scheduler = Scheduler(plan, run_id="test-run-010", use_mock=True)
    scheduler._init_db()
    scheduler.failure_count = 2  # At threshold

    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is True

    plan_row = get_plan(scheduler.db, "test-run-010")
    assert plan_row["status"] == "failed"

    scheduler.db.close()


def test_check_circuit_breaker_pause(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with pause action."""
    monkeypatch.chdir(temp_spawnd_dir)

    cb = CircuitBreaker(threshold=2, action="pause")
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, orchestration=orchestration)

    scheduler = Scheduler(plan, run_id="test-run-011", use_mock=True)
    scheduler._init_db()
    scheduler.failure_count = 2  # At threshold

    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is True

    plan_row = get_plan(scheduler.db, "test-run-011")
    assert plan_row["status"] == "paused"

    scheduler.db.close()


def test_check_circuit_breaker_notify_only(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with notify_only action."""
    monkeypatch.chdir(temp_spawnd_dir)

    cb = CircuitBreaker(threshold=2, action="notify_only")
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, orchestration=orchestration)

    scheduler = Scheduler(plan, run_id="test-run-012", use_mock=True)
    scheduler._init_db()
    scheduler.failure_count = 2  # At threshold

    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is False  # notify_only doesn't stop

    scheduler.db.close()


def test_check_circuit_breaker_below_threshold(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker below threshold."""
    monkeypatch.chdir(temp_spawnd_dir)

    cb = CircuitBreaker(threshold=5, action="cancel_all")
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents, orchestration=orchestration)

    scheduler = Scheduler(plan, run_id="test-run-013", use_mock=True)
    scheduler._init_db()
    scheduler.failure_count = 2  # Below threshold

    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is False

    scheduler.db.close()


def test_check_stuck_uses_latest_event_identity_not_capped_count(temp_spawnd_dir, monkeypatch):
    """Fresh events with a stable capped count should not look stuck."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A")]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id="test-run-stuck-marker", use_mock=True)
    scheduler._init_db()
    scheduler.tasks = {"a": object()}  # any truthy live-task sentinel is enough

    first = [{"id": f"a{i}", "agent": "a", "event_type": "progress", "data": "{}", "ts": "t"} for i in range(50)]
    second = [{"id": f"b{i}", "agent": "a", "event_type": "progress", "data": "{}", "ts": "t"} for i in range(50)]
    events = iter([first, second])

    monkeypatch.setattr("spawnd.runtime.scheduler.get_recent_events", lambda *args, **kwargs: next(events))

    assert scheduler._check_stuck() is False
    assert scheduler.idle_iterations == 0

    assert scheduler._check_stuck() is False
    assert scheduler.idle_iterations == 0

    scheduler.db.close()


@pytest.mark.asyncio
async def test_cost_exceeded_is_terminal_and_not_retried(temp_spawnd_dir, monkeypatch):
    """Per-agent max cost should be terminal, not retried via on_failure=retry."""
    monkeypatch.chdir(temp_spawnd_dir)

    agents = [AgentSpec(name="a", prompt="Task A", on_failure="retry", retry_count=2)]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id="test-run-cost-terminal", use_mock=True)

    spawn_count = 0

    async def fake_spawn_agent(self, agent_row):
        async def finish():
            update_agent_status(
                self.db,
                self.run_id,
                agent_row["name"],
                "cost_exceeded",
                "Cost exceeded: $9.0000",
            )
            return {"success": False, "status": "cost_exceeded", "error": "cost_exceeded"}

        nonlocal spawn_count
        spawn_count += 1
        return asyncio.create_task(finish())

    monkeypatch.setattr(Scheduler, "_spawn_agent", fake_spawn_agent)

    result = await scheduler.run()

    db = open_db("test-run-cost-terminal")
    agent_row = db.execute(
        "SELECT status, retry_attempt FROM agents WHERE run_id = ? AND name = ?",
        ("test-run-cost-terminal", "a"),
    ).fetchone()
    db.close()

    assert spawn_count == 1
    assert result.success is False
    assert result.failed == ["a"]
    assert agent_row["status"] == "cost_exceeded"
    assert agent_row["retry_attempt"] == 0
