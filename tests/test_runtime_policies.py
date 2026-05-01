"""Focused tests for runtime policy helpers."""

from spawnd.runtime.agent_state import transition_agent_status
from spawnd.runtime.policies.budget import apply_run_budget_policy
from spawnd.runtime.policies.circuit_breaker import apply_circuit_breaker_policy
from spawnd.runtime.policies.failure import apply_failure_policy
from spawnd.runtime.policies.stuck import evaluate_stuck_run
from spawnd.storage.db import get_agent, get_plan, init_db, insert_agent, insert_plan


def _seed_run(run_id: str):
    db = init_db(run_id)
    insert_plan(db, run_id, "policy-tests", "name: policy-tests")
    return db


def test_transition_agent_status_respects_force_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-force-transition"
    db = _seed_run(run_id)
    insert_agent(db, run_id, "worker", "do work")
    db.execute(
        "UPDATE agents SET status = ? WHERE run_id = ? AND name = ?",
        ("cancelled", run_id, "worker"),
    )
    db.commit()

    assert transition_agent_status(
        db,
        run_id,
        "worker",
        "completed",
        current_status="cancelled",
        force=False,
    ) is False
    assert get_agent(db, run_id, "worker")["status"] == "cancelled"

    assert transition_agent_status(
        db,
        run_id,
        "worker",
        "completed",
        current_status="cancelled",
        force=True,
    ) is True
    assert get_agent(db, run_id, "worker")["status"] == "completed"
    db.close()


def test_apply_run_budget_policy_warn_does_not_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-budget-warn"
    db = _seed_run(run_id)
    calls: list[tuple[str, str]] = []

    def cancel_all(status: str, message: str = "") -> None:
        calls.append((status, message))

    should_stop = apply_run_budget_policy(
        db,
        run_id,
        total_cost=12.5,
        budget=10.0,
        action="warn",
        cancel_all=cancel_all,
    )

    assert should_stop is False
    assert calls == []
    assert get_plan(db, run_id)["status"] == "running"
    db.close()


def test_apply_circuit_breaker_policy_cancel_all_stops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-cb-cancel"
    db = _seed_run(run_id)
    calls: list[tuple[str, str, bool]] = []

    def cancel_all(status: str, message: str = "", include_pending: bool = False) -> None:
        calls.append((status, message, include_pending))

    should_stop = apply_circuit_breaker_policy(
        db,
        run_id,
        failure_count=3,
        threshold=3,
        action="cancel_all",
        cancel_all=cancel_all,
    )

    assert should_stop is True
    assert calls == [("cancelled", "Circuit breaker tripped", True)]
    assert get_plan(db, run_id)["status"] == "failed"
    db.close()


def test_apply_failure_policy_retry_resets_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-failure-retry"
    db = _seed_run(run_id)
    insert_agent(db, run_id, "worker", "do work", on_failure="retry", retry_count=2)
    db.execute(
        "UPDATE agents SET status = ?, retry_attempt = ? WHERE run_id = ? AND name = ?",
        ("failed", 0, run_id, "worker"),
    )
    db.commit()
    cancel_calls: list[str] = []

    def cancel_all(status: str, message: str = "", include_pending: bool = False) -> None:
        cancel_calls.append(status)

    should_stop = apply_failure_policy(db, run_id, "worker", "boom", cancel_all)
    agent = get_agent(db, run_id, "worker")

    assert should_stop is False
    assert cancel_calls == []
    assert agent["status"] == "pending"
    assert agent["retry_attempt"] == 1
    assert agent["last_error"] == "boom"
    db.close()


def test_evaluate_stuck_run_marks_stuck_at_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "run-stuck"
    db = _seed_run(run_id)
    events = [{"id": "event-1"}]

    is_stuck, marker, idle = evaluate_stuck_run(
        db,
        run_id,
        events=events,
        has_live_tasks=True,
        last_event_marker="event-1",
        idle_iterations=2,
        threshold=3,
    )

    assert is_stuck is True
    assert marker == "event-1"
    assert idle == 3
    db.close()
