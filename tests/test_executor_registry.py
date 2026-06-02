"""Tests for the executor registry."""
import pytest
from spawnd.runtime.executors import claude as _claude_executor_module  # pyright: ignore[reportUnusedImport] # noqa: F401
from spawnd.runtime.executors import codex as _codex_executor_module  # pyright: ignore[reportUnusedImport] # noqa: F401
from spawnd.runtime.executors.base import EXECUTOR_REGISTRY, Executor, ExecutorNotFound, get_executor, register

def test_claude_executor_is_registered():
    ex = get_executor('claude')
    assert ex.runtime == 'claude'
    assert isinstance(ex, Executor)

def test_codex_executor_is_registered():
    ex = get_executor('codex')
    assert ex.runtime == 'codex'
    assert isinstance(ex, Executor)

def test_unknown_runtime_raises_executor_not_found():
    with pytest.raises(ExecutorNotFound):
        _ = get_executor('definitely-not-a-runtime')

def test_custom_register_roundtrip():

    class DummyExecutor(Executor):
        runtime = 'dummy-test-runtime'

        async def run(self, config, toolset):
            return {'success': True, 'status': 'completed'}
    dummy = DummyExecutor()
    try:
        _ = register(dummy)
        assert get_executor('dummy-test-runtime') is dummy
    finally:
        _ = EXECUTOR_REGISTRY.pop('dummy-test-runtime', None)
