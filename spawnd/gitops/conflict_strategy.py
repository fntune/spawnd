"""Merge conflict handling strategies."""
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class ConflictContext:
    """Context passed to conflict strategy handlers."""
    name: str
    branch: str
    conflict_files: list[str]

class ConflictStrategy:
    """Strategy object for merge conflict decisions."""

    def handle(self, ctx: ConflictContext, *, try_resolve: Callable[[], bool], abort_merge: Callable[[], object]) -> tuple[bool, dict[str, object]]:
        """Return (should_continue, failure_payload_or_empty)."""
        raise NotImplementedError

class FailOnConflict(ConflictStrategy):

    def handle(self, ctx: ConflictContext, *, try_resolve: Callable[[], bool], abort_merge: Callable[[], object]) -> tuple[bool, dict[str, object]]:
        _ = abort_merge()
        return (True, {'name': ctx.name, 'error': 'conflict', 'files': ctx.conflict_files})

class ManualConflict(ConflictStrategy):

    def handle(self, ctx: ConflictContext, *, try_resolve: Callable[[], bool], abort_merge: Callable[[], object]) -> tuple[bool, dict[str, object]]:
        return (False, {'name': ctx.name, 'error': 'conflict_manual', 'files': ctx.conflict_files})

class SpawnResolverConflict(ConflictStrategy):

    def handle(self, ctx: ConflictContext, *, try_resolve: Callable[[], bool], abort_merge: Callable[[], object]) -> tuple[bool, dict[str, object]]:
        if try_resolve():
            return (True, {})
        _ = abort_merge()
        return (True, {'name': ctx.name, 'error': 'resolver_failed', 'files': ctx.conflict_files})

def strategy_for_mode(mode: str) -> ConflictStrategy:
    """Build a strategy object from ``on_conflict`` mode."""
    if mode == 'fail':
        return FailOnConflict()
    if mode == 'spawn_resolver':
        return SpawnResolverConflict()
    return ManualConflict()
