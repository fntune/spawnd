"""Executor ABC and runtime registry.

An ``Executor`` drives one vendor's agent SDK. Given an ``AgentConfig``
(what to run) and a ``Toolset`` (what tools the agent may use), the
executor spawns the vendor client, streams iterations, persists status
and cost to SQLite, and returns a result dict the scheduler consumes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, TYPE_CHECKING

if TYPE_CHECKING:
    from spawnd.runtime.executor import AgentConfig
    from spawnd.tools.toolset import Toolset


class ExecutorNotFound(LookupError):
    """Raised when no executor is registered for a requested runtime."""


class Executor(ABC):
    """Vendor-neutral executor contract.

    Subclasses declare the ``runtime`` identifier they handle and implement
    ``run(config, toolset) -> dict``. The dict keys expected by the
    scheduler are ``success: bool``, ``status: str``, ``cost: float``,
    and optionally ``error: str`` and ``vendor_session_id: str | None``.
    """

    runtime: ClassVar[str]

    @abstractmethod
    async def run(self, config: AgentConfig, toolset: Toolset) -> dict: ...


EXECUTOR_REGISTRY: dict[str, Executor] = {}


def register(executor: Executor) -> None:
    """Register an executor under its declared runtime name."""
    EXECUTOR_REGISTRY[executor.runtime] = executor


def get_executor(runtime: str) -> Executor:
    """Look up the executor for a runtime name."""
    try:
        return EXECUTOR_REGISTRY[runtime]
    except KeyError as err:
        available = ", ".join(sorted(EXECUTOR_REGISTRY)) or "<none>"
        raise ExecutorNotFound(
            f"No executor registered for runtime {runtime!r}. Available: {available}"
        ) from err
