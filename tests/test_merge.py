"""Tests for merge module - branch merging utilities."""
import subprocess

import pytest
from spawnd.storage.db import init_db, insert_agent, update_agent_status

from tests.helpers import insert_test_plan
from spawnd.gitops.merge import get_merge_order, merge_run, check_conflicts, squash_merge

@pytest.fixture
def temp_spawnd_dir(tmp_path, monkeypatch):
    """Create a temporary .spawnd runs directory."""
    runs_dir = tmp_path / '.spawnd' / 'runs'
    _ = runs_dir.mkdir(parents=True)
    _ = monkeypatch.chdir(tmp_path)
    return tmp_path

@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository."""
    repo = tmp_path / 'repo'
    _ = repo.mkdir()
    _ = subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True, capture_output=True)
    _ = (repo / 'README.md').write_text('# Test Repo')
    _ = subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=repo, check=True, capture_output=True)
    return repo

def test_get_merge_order_no_completed(temp_spawnd_dir):
    """Test get_merge_order with no completed agents."""
    run_id = 'test-merge-order-1'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="running")
    _ = insert_agent(db, run_id, 'agent1', 'Test')
    _ = db.close()
    order = get_merge_order(run_id)
    assert order == []

def test_get_merge_order_single(temp_spawnd_dir):
    """Test get_merge_order with single completed agent."""
    run_id = 'test-merge-order-2'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Test')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    order = get_merge_order(run_id)
    assert order == ['agent1']

def test_get_merge_order_with_deps(temp_spawnd_dir):
    """Test get_merge_order respects dependencies."""
    run_id = 'test-merge-order-3'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'auth', 'Auth task')
    _ = insert_agent(db, run_id, 'tests', 'Test task', depends_on=['auth'])
    _ = update_agent_status(db, run_id, 'auth', 'completed')
    _ = update_agent_status(db, run_id, 'tests', 'completed')
    _ = db.close()
    order = get_merge_order(run_id)
    assert 'auth' in order
    assert 'tests' in order
    assert order.index('auth') < order.index('tests')

def test_merge_run_no_completed(temp_spawnd_dir):
    """Test merge_run with no completed agents."""
    run_id = 'test-merge-run-1'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="running")
    _ = insert_agent(db, run_id, 'agent1', 'Test')
    _ = db.close()
    result = merge_run(run_id, cleanup=False)
    assert result['merged'] == []
    assert result['failed'] == []

def test_merge_run_skips_no_branch(temp_spawnd_dir):
    """Test merge_run skips agents without branch."""
    run_id = 'test-merge-run-2'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Test')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    result = merge_run(run_id, cleanup=False)
    assert 'agent1' in result['skipped']

def test_check_conflicts_no_agents(temp_spawnd_dir):
    """Test check_conflicts with no completed agents."""
    run_id = 'test-conflicts-1'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="running")
    _ = db.close()
    conflicts = check_conflicts(run_id)
    assert conflicts == []

def test_squash_merge(git_repo, monkeypatch):
    """Test squash_merge function."""
    _ = monkeypatch.chdir(git_repo)
    _ = subprocess.run(['git', 'checkout', '-b', 'feature'], cwd=git_repo, check=True, capture_output=True)
    _ = (git_repo / 'feature.txt').write_text('feature content')
    _ = subprocess.run(['git', 'add', '.'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Add feature'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'checkout', '-'], cwd=git_repo, check=True, capture_output=True)
    _ = squash_merge('feature', 'Squashed feature')
    assert (git_repo / 'feature.txt').exists()
    result = subprocess.run(['git', 'log', '--oneline'], cwd=git_repo, capture_output=True, text=True)
    lines = [l for l in result.stdout.strip().split('\n') if l]
    assert len(lines) == 2
    assert 'Squashed feature' in result.stdout

def test_check_conflicts_empty_completed(temp_spawnd_dir):
    """Test check_conflicts with completed agents but no branches."""
    run_id = 'test-conflicts-empty'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Task 1')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    conflicts = check_conflicts(run_id)
    assert conflicts == []

def test_merge_run_on_conflict_fail(temp_spawnd_dir):
    """Test merge_run with on_conflict=fail returns correct result."""
    run_id = 'test-merge-fail'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Task 1')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.close()
    result = merge_run(run_id, cleanup=False, on_conflict='fail')
    assert 'agent1' in result['skipped']
    assert result['merged'] == []
    assert result['resolved'] == []

def test_merge_run_conflict_does_not_mark_agent_merged(temp_spawnd_dir, monkeypatch):
    """merge_run should treat False from merge_branch_to_current as a conflict."""
    run_id = 'test-merge-conflict'
    db = init_db(run_id)
    insert_test_plan(db, run_id, "test", status="completed")
    _ = insert_agent(db, run_id, 'agent1', 'Task 1')
    _ = update_agent_status(db, run_id, 'agent1', 'completed')
    _ = db.execute('UPDATE agents SET branch = ? WHERE run_id = ? AND name = ?', ('spawnd/test/agent1', run_id, 'agent1'))
    _ = db.commit()
    _ = db.close()
    _ = monkeypatch.setattr('spawnd.gitops.merge.merge_branch_to_current', lambda branch: False)
    _ = monkeypatch.setattr('spawnd.gitops.merge.get_conflict_files', lambda: ['conflicted.py'])
    result = merge_run(run_id, cleanup=False, on_conflict='fail')
    assert result['merged'] == []
    assert result['failed'] == [{'name': 'agent1', 'error': 'conflict', 'files': ['conflicted.py']}]

def test_get_conflict_files_no_conflicts(git_repo, monkeypatch):
    """Test get_conflict_files when no conflicts exist."""
    from spawnd.gitops.merge import get_conflict_files
    _ = monkeypatch.chdir(git_repo)
    files = get_conflict_files()
    assert files == []
