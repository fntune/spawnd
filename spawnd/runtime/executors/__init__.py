"""Pluggable executors — one per vendor runtime.

Adapters self-register at import time. Importing ``spawnd.runtime.executors``
triggers the Claude and Codex adapter registrations; optional ``[openai]``
installs enable the OpenAI adapter.
"""

from spawnd.runtime.executors.base import (
    EXECUTOR_REGISTRY,
    Executor,
    ExecutorNotFound,
    get_executor,
    register,
)

from spawnd.runtime.executors import claude  # pyright: ignore[reportUnusedImport] # noqa: F401
from spawnd.runtime.executors import codex  # pyright: ignore[reportUnusedImport] # noqa: F401

try:
    from spawnd.runtime.executors import openai  # pyright: ignore[reportUnusedImport] # noqa: F401
except ImportError:
    pass

__all__ = [
    "EXECUTOR_REGISTRY",
    "Executor",
    "ExecutorNotFound",
    "get_executor",
    "register",
]
