"""Tests for merge conflict strategy selection and behavior."""

from spawnd.gitops.conflict_strategy import (
    ConflictContext,
    FailOnConflict,
    ManualConflict,
    SpawnResolverConflict,
    strategy_for_mode,
)


def test_strategy_for_mode_selects_expected_strategy():
    assert isinstance(strategy_for_mode("fail"), FailOnConflict)
    assert isinstance(strategy_for_mode("spawn_resolver"), SpawnResolverConflict)
    assert isinstance(strategy_for_mode("manual"), ManualConflict)
    assert isinstance(strategy_for_mode("unknown"), ManualConflict)


def test_fail_on_conflict_aborts_merge_and_reports_failure():
    strategy = FailOnConflict()
    ctx = ConflictContext(name="agent-a", branch="spawnd/run/agent-a", conflict_files=["a.py"])
    calls: list[str] = []

    should_continue, failure = strategy.handle(
        ctx,
        try_resolve=lambda: True,
        abort_merge=lambda: calls.append("abort"),
    )

    assert should_continue is True
    assert calls == ["abort"]
    assert failure == {"name": "agent-a", "error": "conflict", "files": ["a.py"]}


def test_spawn_resolver_conflict_success_skips_abort():
    strategy = SpawnResolverConflict()
    ctx = ConflictContext(name="agent-b", branch="spawnd/run/agent-b", conflict_files=["b.py"])
    calls: list[str] = []

    should_continue, failure = strategy.handle(
        ctx,
        try_resolve=lambda: True,
        abort_merge=lambda: calls.append("abort"),
    )

    assert should_continue is True
    assert failure == {}
    assert calls == []


def test_spawn_resolver_conflict_failure_aborts_and_reports():
    strategy = SpawnResolverConflict()
    ctx = ConflictContext(name="agent-c", branch="spawnd/run/agent-c", conflict_files=["c.py"])
    calls: list[str] = []

    should_continue, failure = strategy.handle(
        ctx,
        try_resolve=lambda: False,
        abort_merge=lambda: calls.append("abort"),
    )

    assert should_continue is True
    assert calls == ["abort"]
    assert failure == {"name": "agent-c", "error": "resolver_failed", "files": ["c.py"]}
