"""Runtime isolation policy tests."""
from __future__ import annotations

import pytest

from spawnd.policy.isolation import RuntimeIsolationError, validate_runtime_isolation


def test_write_runtime_requires_declared_isolation():
    with pytest.raises(RuntimeIsolationError, match='SPAWND_RUNTIME_ISOLATION'):
        validate_runtime_isolation(
            runtime='claude',
            agent_type='worker',
            write_allowed=True,
            use_mock=False,
            policy=None,
            env={},
        )


def test_readonly_and_mock_runtimes_do_not_require_isolation():
    validate_runtime_isolation(
        runtime='claude',
        agent_type='worker',
        write_allowed=False,
        use_mock=False,
        policy=None,
        env={},
    )
    validate_runtime_isolation(
        runtime='claude',
        agent_type='worker',
        write_allowed=True,
        use_mock=True,
        policy=None,
        env={},
    )


def test_declared_container_isolation_allows_write_runtime():
    validate_runtime_isolation(
        runtime='openai',
        agent_type='worker',
        write_allowed=True,
        use_mock=False,
        policy=None,
        env={'SPAWND_RUNTIME_ISOLATION': 'container'},
    )
