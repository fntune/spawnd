"""Tests for newly extracted runtime architecture seams."""

from spawnd.runtime.agent_config import resolve_agent_plan_config
from spawnd.runtime.agent_state import can_transition
from spawnd.runtime.run_state import run_has_persisted_plan
from spawnd.runtime.task_registry import CancellationRegistry
from spawnd.models.specs import AgentSpec, Defaults
from spawnd.storage.db import init_db, insert_plan


def test_resolve_agent_plan_config_uses_agent_overrides():
    defaults = Defaults(check="pytest", model="sonnet", runtime="claude", retry_count=3)
    agent = AgentSpec(
        name="worker",
        prompt="Implement task",
        check="true",
        model="haiku",
        max_iterations=7,
        max_cost_usd=1.5,
        on_failure="retry",
        retry_count=1,
        runtime="openai",
    )

    resolved = resolve_agent_plan_config(agent, defaults)

    assert resolved.check_command == "true"
    assert resolved.model == "haiku"
    assert resolved.max_iterations == 7
    assert resolved.max_cost_usd == 1.5
    assert resolved.retry_count == 1
    assert resolved.runtime == "openai"
    assert resolved.cost_source == "estimated"


def test_resolve_agent_plan_config_prefers_role_defaults_over_plan_defaults():
    defaults = Defaults(check="pytest", model="sonnet", runtime="claude", retry_count=3)
    agent = AgentSpec(
        name="worker",
        prompt="Implement task",
        use_role="tester",
    )

    resolved = resolve_agent_plan_config(agent, defaults)

    assert resolved.check_command == "pytest tests/ -v"
    assert resolved.model == "sonnet"
    assert "## Your Task" in resolved.prompt


def test_agent_state_machine_rejects_cancelled_to_completed():
    assert can_transition("cancelled", "completed") is False
    assert can_transition("running", "completed") is True


def test_run_has_persisted_plan_validates_plan_table(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-seam-check"
    db = init_db(run_id)
    insert_plan(db, run_id, "test", "name: test", 25.0)
    db.close()

    assert run_has_persisted_plan(run_id) is True


def test_cancellation_registry_is_run_scoped():
    registry = CancellationRegistry()

    class DummyTask:
        def __init__(self):
            self.cancelled = False
            self._done = False

        def done(self):
            return self._done

        def cancel(self):
            self.cancelled = True

    task_a = DummyTask()
    task_b = DummyTask()
    registry.register("run-a", "agent-1", task_a)  # type: ignore[arg-type]
    registry.register("run-b", "agent-1", task_b)  # type: ignore[arg-type]

    assert registry.cancel("run-a", "agent-1") is True
    assert task_a.cancelled is True
    assert task_b.cancelled is False

    registry.clear_run("run-b")
    assert registry.get("run-b", "agent-1") is None
