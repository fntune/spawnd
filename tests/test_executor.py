"""Tests for executor module - agent execution."""
from pathlib import Path

import pytest
from spawnd.storage.db import get_agent, init_db, insert_agent, open_db
from spawnd.runtime.executor import AgentConfig, build_manager_system_prompt, build_system_prompt, run_worker_mock

from tests.helpers import require_row

@pytest.fixture
def temp_spawnd_dir(tmp_path):
    """Create a temporary .spawnd runs directory."""
    runs_dir = tmp_path / '.spawnd' / 'runs'
    _ = runs_dir.mkdir(parents=True)
    return tmp_path

def test_agent_config_tree_path():
    """Test AgentConfig.tree_path() method."""
    config = AgentConfig(name='child', run_id='run-1', prompt='Test', worktree=Path('/tmp/test'), parent='parent')
    assert config.tree_path() == 'parent.child'
    config_no_parent = AgentConfig(name='root', run_id='run-1', prompt='Test', worktree=Path('/tmp/test'))
    assert config_no_parent.tree_path() == 'root'

def test_agent_config_tree_path_does_not_duplicate_parent_prefix():
    """Manager-spawned workers already use fully qualified names."""
    config = AgentConfig(name='manager.child', run_id='run-1', prompt='Test', worktree=Path('/tmp/test'), parent='manager')
    assert config.tree_path() == 'manager.child'

def test_build_system_prompt():
    """Test system prompt building."""
    config = AgentConfig(name='test', run_id='run-1', prompt='Implement feature X', worktree=Path('/tmp/test'), check_command='pytest', shared_context='This is shared context.')
    prompt = build_system_prompt(config)
    assert 'Implement feature X' in prompt
    assert 'pytest' in prompt
    assert 'This is shared context.' in prompt

def test_build_system_prompt_no_shared_context():
    """Test system prompt without shared context."""
    config = AgentConfig(name='test', run_id='run-1', prompt='Do something', worktree=Path('/tmp/test'))
    prompt = build_system_prompt(config)
    assert 'Do something' in prompt
    assert 'true' in prompt

def test_build_manager_system_prompt_includes_shared_context():
    """Manager prompts should receive shared context too."""
    config = AgentConfig(name='manager', run_id='run-1', prompt='Coordinate work', worktree=Path('/tmp/test'), shared_context='Shared design notes.')
    prompt = build_manager_system_prompt(config)
    assert 'Coordinate work' in prompt
    assert 'Shared design notes.' in prompt

@pytest.mark.asyncio
async def test_run_worker_mock_success(temp_spawnd_dir, monkeypatch):
    """Test mock worker with passing check."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    run_id = 'test-run-mock-1'
    db = init_db(run_id)
    _ = insert_agent(db, run_id, 'test-agent', 'Test task', check_command='true')
    _ = db.close()
    worktree = temp_spawnd_dir / '.spawnd' / 'runs' / run_id / 'worktrees' / 'test-agent'
    _ = worktree.mkdir(parents=True)
    config = AgentConfig(name='test-agent', run_id=run_id, prompt='Test task', worktree=worktree, check_command='true')
    result = await run_worker_mock(config)
    assert result['success'] is True
    assert result['status'] == 'completed'
    db = open_db(run_id)
    agent = require_row(get_agent(db, run_id, 'test-agent'))
    assert agent['status'] == 'completed'
    _ = db.close()

@pytest.mark.asyncio
async def test_run_worker_mock_failure(temp_spawnd_dir, monkeypatch):
    """Test mock worker with failing check."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    run_id = 'test-run-mock-2'
    db = init_db(run_id)
    _ = insert_agent(db, run_id, 'test-agent', 'Test task', check_command='false')
    _ = db.close()
    worktree = temp_spawnd_dir / '.spawnd' / 'runs' / run_id / 'worktrees' / 'test-agent'
    _ = worktree.mkdir(parents=True)
    config = AgentConfig(name='test-agent', run_id=run_id, prompt='Test task', worktree=worktree, check_command='false')
    result = await run_worker_mock(config)
    assert result['success'] is False
    assert result['status'] == 'failed'
    db = open_db(run_id)
    agent = require_row(get_agent(db, run_id, 'test-agent'))
    assert agent['status'] == 'failed'
    _ = db.close()

def test_agent_config_defaults():
    """Test AgentConfig default values."""
    config = AgentConfig(name='test', run_id='run-1', prompt='Test', worktree=Path('/tmp/test'))
    assert config.check_command == 'true'
    assert config.model == 'sonnet'
    assert config.max_iterations == 30
    assert config.max_cost_usd == 5.0
    assert config.parent is None
    assert config.env is None
    assert config.shared_context == ''
