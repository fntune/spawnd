"""Tests for database operations."""

import tempfile
from pathlib import Path

import pytest

from spawnd.storage.db import (
    get_agent,
    get_agents,
    get_pending_agents,
    get_plan,
    init_db,
    insert_agent,
    insert_event,
    insert_plan,
    update_agent_status,
)


@pytest.fixture
def tmp_run(tmp_path):
    """Create a temporary run with initialized DB."""
    run_id = "test-run"
    # Override the base path by setting up in tmp_path
    base = tmp_path / ".spawnd" / "runs" / run_id
    base.mkdir(parents=True)

    db = init_db(run_id, base_path=tmp_path)
    yield run_id, db, tmp_path
    db.close()


def test_init_db(tmp_path):
    """Test database initialization."""
    db = init_db("test", base_path=tmp_path)
    assert db is not None

    # Check tables exist
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "plans" in tables
    assert "agents" in tables
    assert "events" in tables
    assert "responses" in tables

    db.close()


def test_insert_and_get_plan(tmp_path):
    """Test plan insertion and retrieval."""
    db = init_db("test", base_path=tmp_path)

    insert_plan(db, "test", "Test Plan", "yaml: content", 25.0)
    plan = get_plan(db, "test")

    assert plan is not None
    assert plan["name"] == "Test Plan"
    assert plan["budget_usd"] == 25.0

    db.close()


def test_insert_and_get_agent(tmp_path):
    """Test agent insertion and retrieval."""
    db = init_db("test", base_path=tmp_path)
    insert_plan(db, "test", "Test", "", 25.0)

    insert_agent(
        db,
        "test",
        "worker1",
        "Do task",
        agent_type="worker",
        check_command="pytest",
        model="sonnet",
    )

    agent = get_agent(db, "test", "worker1")
    assert agent is not None
    assert agent["name"] == "worker1"
    assert agent["prompt"] == "Do task"
    assert agent["status"] == "pending"

    db.close()


def test_get_pending_agents(tmp_path):
    """Test getting pending agents with satisfied dependencies."""
    db = init_db("test", base_path=tmp_path)
    insert_plan(db, "test", "Test", "", 25.0)

    # a has no deps, b depends on a
    insert_agent(db, "test", "a", "Task A")
    insert_agent(db, "test", "b", "Task B", depends_on=["a"])

    # Initially only a is pending with satisfied deps
    pending = get_pending_agents(db, "test")
    assert len(pending) == 1
    assert pending[0]["name"] == "a"

    # After a completes, b becomes pending
    update_agent_status(db, "test", "a", "completed")
    pending = get_pending_agents(db, "test")
    assert len(pending) == 1
    assert pending[0]["name"] == "b"

    db.close()


def test_update_agent_status(tmp_path):
    """Test agent status updates."""
    db = init_db("test", base_path=tmp_path)
    insert_plan(db, "test", "Test", "", 25.0)
    insert_agent(db, "test", "worker1", "Task")

    update_agent_status(db, "test", "worker1", "running")
    agent = get_agent(db, "test", "worker1")
    assert agent["status"] == "running"

    update_agent_status(db, "test", "worker1", "failed", "Some error")
    agent = get_agent(db, "test", "worker1")
    assert agent["status"] == "failed"
    assert agent["error"] == "Some error"

    db.close()


def test_insert_event(tmp_path):
    """Test event insertion."""
    db = init_db("test", base_path=tmp_path)
    insert_plan(db, "test", "Test", "", 25.0)
    insert_agent(db, "test", "worker1", "Task")

    insert_event(db, "test", "worker1", "started", {"prompt": "Do task"})

    cursor = db.execute("SELECT * FROM events WHERE run_id = 'test'")
    events = cursor.fetchall()
    assert len(events) == 1

    db.close()
