"""Tests for coordination tools."""

import asyncio

import pytest

from spawnd.storage.db import (
    get_agent,
    get_response,
    init_db,
    insert_agent,
    insert_event,
    insert_plan,
    update_agent_status,
)
from spawnd.tools.worker import mark_complete, report_progress
from spawnd.tools.manager import (
    mark_plan_complete,
    respond_to_clarification,
    spawn_worker,
)


@pytest.fixture
def temp_spawnd_dir(tmp_path, monkeypatch):
    """Create a temporary .spawnd runs directory."""
    runs_dir = tmp_path / ".spawnd" / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_mark_complete_agent_not_found(temp_spawnd_dir):
    """Test mark_complete with non-existent agent."""
    run_id = "test-mark-complete-1"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    db.close()

    result = asyncio.run(mark_complete(run_id, "nonexistent", "Done"))

    assert "ERROR" in result
    assert "not found" in result


def test_mark_complete_check_passes(temp_spawnd_dir):
    """Test mark_complete when check command passes."""
    run_id = "test-mark-complete-2"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "worker1", "Test task", check_command="true")
    worktree = temp_spawnd_dir / ".spawnd" / "runs" / run_id / "worktrees" / "worker1"
    worktree.mkdir(parents=True)
    db.execute(
        "UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?",
        (str(worktree), run_id, "worker1")
    )
    db.commit()
    db.close()

    result = asyncio.run(mark_complete(run_id, "worker1", "Task completed"))

    assert "completed successfully" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "worker1")
    db.close()
    assert agent["status"] == "completed"


def test_mark_complete_check_fails(temp_spawnd_dir):
    """Test mark_complete when check command fails."""
    run_id = "test-mark-complete-3"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "worker1", "Test task", check_command="false")
    worktree = temp_spawnd_dir / ".spawnd" / "runs" / run_id / "worktrees" / "worker1"
    worktree.mkdir(parents=True)
    db.execute(
        "UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?",
        (str(worktree), run_id, "worker1")
    )
    db.commit()
    db.close()

    result = asyncio.run(mark_complete(run_id, "worker1", "Done"))

    assert "Check failed" in result


def test_report_progress(temp_spawnd_dir):
    """Test report_progress creates event."""
    run_id = "test-progress-1"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "worker1", "Test task")
    db.close()

    result = asyncio.run(report_progress(run_id, "worker1", "50% complete", "halfway"))

    assert "Progress recorded" in result


def test_spawn_worker_success(temp_spawnd_dir):
    """Test spawn_worker creates new agent."""
    run_id = "test-spawn-1"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "task1", "Do something", "pytest", "sonnet"))

    assert "Spawned worker: manager.task1" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "manager.task1")
    db.close()
    assert agent is not None
    assert agent["parent"] == "manager"
    assert agent["prompt"] == "Do something"


def test_spawn_worker_already_exists(temp_spawnd_dir):
    """Test spawn_worker fails if agent already exists."""
    run_id = "test-spawn-2"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "manager.task1", "Existing", parent="manager")
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "task1", "Do something"))

    assert "already exists" in result


def test_spawn_worker_rejects_invalid_name(temp_spawnd_dir):
    """Worker names should be validated before touching DB state."""
    run_id = "test-spawn-invalid"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "../oops", "Do something"))

    assert "Invalid worker name" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "manager.../oops")
    db.close()
    assert agent is None


def test_cancel_worker_cannot_cancel_unowned_agent(temp_spawnd_dir):
    """Managers should only be able to cancel their own workers."""
    from spawnd.tools.manager import cancel_worker

    run_id = "test-cancel-scope"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "victim", "Running task")
    update_agent_status(db, run_id, "victim", "running")
    db.close()

    result = asyncio.run(cancel_worker(run_id, "manager", "victim"))

    assert "Worker not found" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "victim")
    db.close()
    assert agent["status"] == "running"


def test_get_worker_status_cannot_read_unowned_agent(temp_spawnd_dir):
    """Managers should not inspect unrelated agents."""
    from spawnd.tools.manager import get_worker_status

    run_id = "test-status-scope"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "victim", "Task")
    db.close()

    result = asyncio.run(get_worker_status(run_id, "manager", "victim"))
    assert "Worker not found" in result


def test_respond_to_clarification(temp_spawnd_dir):
    """Test respond_to_clarification creates response."""
    run_id = "test-respond-1"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers")
    insert_agent(db, run_id, "manager.task1", "Worker task", parent="manager")
    clarification_id = insert_event(db, run_id, "manager.task1", "clarification", {"question": "Use JWT?"})
    db.close()

    result = asyncio.run(respond_to_clarification(run_id, "manager", clarification_id, "Use JWT"))

    assert "Response sent" in result

    db = init_db(run_id)
    response = get_response(db, run_id, clarification_id)
    db.close()
    assert response is not None
    assert response["response"] == "Use JWT"


def test_respond_to_clarification_rejects_unowned_request(temp_spawnd_dir):
    """Managers should not respond to other managers' clarifications."""
    run_id = "test-respond-scope"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers")
    insert_agent(db, run_id, "other.task1", "Worker task", parent="other")
    clarification_id = insert_event(db, run_id, "other.task1", "clarification", {"question": "Need input"})
    db.close()

    result = asyncio.run(respond_to_clarification(run_id, "manager", clarification_id, "No"))

    assert "Clarification not found" in result

    db = init_db(run_id)
    response = get_response(db, run_id, clarification_id)
    db.close()
    assert response is None


def test_mark_plan_complete_workers_pending(temp_spawnd_dir):
    """Test mark_plan_complete fails if workers still running."""
    run_id = "test-plan-complete-1"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "manager.task1", "Running task", parent="manager")
    update_agent_status(db, run_id, "manager.task1", "running")
    db.close()

    result = asyncio.run(mark_plan_complete(run_id, "manager", "All done"))

    assert "Cannot complete" in result
    assert "still running" in result


def test_mark_plan_complete_success(temp_spawnd_dir):
    """Test mark_plan_complete when all workers done."""
    run_id = "test-plan-complete-2"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "manager.task1", "Task 1", parent="manager")
    update_agent_status(db, run_id, "manager.task1", "completed")
    db.close()

    result = asyncio.run(mark_plan_complete(run_id, "manager", "All done"))

    assert "Plan complete" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "manager")
    db.close()
    assert agent["status"] == "completed"


def test_cancel_worker_cancels_live_task(temp_spawnd_dir):
    """cancel_worker should cancel the registered asyncio task, not just flip status."""
    from spawnd.tools.manager import cancel_worker
    from spawnd.runtime import task_registry

    run_id = "test-cancel-live"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage", agent_type="manager")
    insert_agent(db, run_id, "manager.victim", "work", parent="manager")
    update_agent_status(db, run_id, "manager.victim", "running")
    db.close()

    async def _run():
        async def long_running():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(long_running())
        task_registry.register(run_id, "manager.victim", task)
        try:
            result = await cancel_worker(run_id, "manager", "victim")
            await asyncio.sleep(0)  # let cancellation propagate
            return result, task
        finally:
            task_registry.unregister(run_id, "manager.victim")

    result, task = asyncio.run(_run())

    assert "Cancelled worker" in result
    assert task.cancelled() or task.done()

    db = init_db(run_id)
    agent = get_agent(db, run_id, "manager.victim")
    db.close()
    assert agent["status"] == "cancelled"


def test_cancel_worker_preserves_other_terminal_states(temp_spawnd_dir):
    """cancel_worker must not overwrite timeout/cost terminal reasons."""
    from spawnd.tools.manager import cancel_worker

    run_id = "test-cancel-terminal-state"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage", agent_type="manager")
    insert_agent(db, run_id, "manager.victim", "work", parent="manager")
    update_agent_status(db, run_id, "manager.victim", "cost_exceeded", "budget")
    db.close()

    result = asyncio.run(cancel_worker(run_id, "manager", "victim"))

    assert "already in terminal state" in result

    db = init_db(run_id)
    agent = get_agent(db, run_id, "manager.victim")
    db.close()
    assert agent["status"] == "cost_exceeded"


def test_mark_complete_refuses_cancelled_agent(temp_spawnd_dir):
    """A cancelled agent must not be able to flip itself back to completed."""
    from spawnd.tools.worker import mark_complete as worker_mark_complete

    run_id = "test-cancelled-mark-complete"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "worker1", "Test task", check_command="true")
    worktree = temp_spawnd_dir / ".spawnd" / "runs" / run_id / "worktrees" / "worker1"
    worktree.mkdir(parents=True)
    db.execute(
        "UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?",
        (str(worktree), run_id, "worker1"),
    )
    db.commit()
    update_agent_status(db, run_id, "worker1", "cancelled", "cancelled by manager")
    db.close()

    result = asyncio.run(worker_mark_complete(run_id, "worker1", "Task done"))

    text = result
    assert "ERROR" in text
    assert "terminal state" in text

    db = init_db(run_id)
    agent = get_agent(db, run_id, "worker1")
    db.close()
    assert agent["status"] == "cancelled"


def test_mark_plan_complete_accepts_cost_exceeded_workers(temp_spawnd_dir):
    """Managers should be able to finish when only terminal cost failures remain."""
    run_id = "test-plan-complete-cost-terminal"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage workers", agent_type="manager")
    insert_agent(db, run_id, "manager.task1", "Task 1", parent="manager")
    update_agent_status(db, run_id, "manager.task1", "cost_exceeded", "budget")
    db.close()

    result = asyncio.run(mark_plan_complete(run_id, "manager", "All done"))

    text = result
    assert "Plan complete" in text
    assert "Failed workers: ['task1']" in text


def test_spawn_worker_enforces_max_subagents(temp_spawnd_dir):
    """spawn_worker must reject further spawns once max_subagents is reached."""
    run_id = "test-max-subagents"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage", agent_type="manager", max_subagents=2)
    insert_agent(db, run_id, "manager.w1", "first", parent="manager")
    insert_agent(db, run_id, "manager.w2", "second", parent="manager")
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "w3", "third"))

    text = result
    assert "max_subagents" in text

    db = init_db(run_id)
    rejected = get_agent(db, run_id, "manager.w3")
    db.close()
    assert rejected is None


def test_spawn_worker_allows_under_max_subagents(temp_spawnd_dir):
    """spawn_worker still succeeds while under max_subagents."""
    run_id = "test-max-subagents-ok"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(db, run_id, "manager", "Manage", agent_type="manager", max_subagents=3)
    insert_agent(db, run_id, "manager.w1", "first", parent="manager")
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "w2", "second"))
    assert "Spawned worker: manager.w2" in result


def test_spawn_worker_inherits_manager_runtime_and_cost_source(temp_spawnd_dir):
    """Manager-spawned workers should stay on the manager's runtime."""
    run_id = "test-spawn-runtime-inherit"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    insert_agent(
        db,
        run_id,
        "manager",
        "Manage",
        agent_type="manager",
        runtime="openai",
        cost_source="estimated",
    )
    db.close()

    result = asyncio.run(spawn_worker(run_id, "manager", "child", "Do work"))
    assert "Spawned worker: manager.child" in result

    db = init_db(run_id)
    worker = get_agent(db, run_id, "manager.child")
    db.close()

    assert worker["runtime"] == "openai"
    assert worker["cost_source"] == "estimated"
