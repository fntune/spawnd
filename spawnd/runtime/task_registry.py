"""Cancellation registry for live agent asyncio tasks."""

from __future__ import annotations

import asyncio


class CancellationRegistry:
    """Tracks cancellable tasks by (run_id, agent_name)."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    def register(self, run_id: str, agent_name: str, task: asyncio.Task) -> None:
        self._tasks[(run_id, agent_name)] = task

    def unregister(self, run_id: str, agent_name: str) -> None:
        self._tasks.pop((run_id, agent_name), None)

    def get(self, run_id: str, agent_name: str) -> asyncio.Task | None:
        return self._tasks.get((run_id, agent_name))

    def cancel(self, run_id: str, agent_name: str) -> bool:
        """Cancel the task if present and still running. Returns True if cancelled."""
        task = self._tasks.get((run_id, agent_name))
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def clear_run(self, run_id: str) -> None:
        for key in [k for k in self._tasks if k[0] == run_id]:
            self._tasks.pop(key, None)


DEFAULT_REGISTRY = CancellationRegistry()


def get_default_registry() -> CancellationRegistry:
    return DEFAULT_REGISTRY


def register(run_id: str, agent_name: str, task: asyncio.Task) -> None:
    DEFAULT_REGISTRY.register(run_id, agent_name, task)


def unregister(run_id: str, agent_name: str) -> None:
    DEFAULT_REGISTRY.unregister(run_id, agent_name)


def get(run_id: str, agent_name: str) -> asyncio.Task | None:
    return DEFAULT_REGISTRY.get(run_id, agent_name)


def cancel(run_id: str, agent_name: str) -> bool:
    return DEFAULT_REGISTRY.cancel(run_id, agent_name)


def clear_run(run_id: str) -> None:
    DEFAULT_REGISTRY.clear_run(run_id)
