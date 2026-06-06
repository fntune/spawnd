"""Tests for git module - worktree operations."""
import subprocess
import pytest
from spawnd.gitops.worktrees import create_worktree, get_current_branch, merge_branch, remove_worktree, run_git, run_worktree_setup, setup_worktree_with_deps

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

def test_get_current_branch(git_repo):
    """Test getting current branch name."""
    branch = get_current_branch(git_repo)
    assert branch in ('main', 'master')

def test_create_worktree(git_repo, monkeypatch):
    """Test worktree creation."""
    _ = monkeypatch.chdir(git_repo)
    worktree_path = create_worktree('test-run', 'agent1', git_repo)
    assert worktree_path.exists()
    assert (worktree_path / '.git').exists()
    assert worktree_path.name == 'agent1'

def test_create_worktree_reuse_existing(git_repo, monkeypatch):
    """Test that existing worktree is reused (resume scenario)."""
    _ = monkeypatch.chdir(git_repo)
    worktree_path1 = create_worktree('test-run', 'agent1', git_repo)
    _ = (worktree_path1 / 'test_file.txt').write_text('test content')
    worktree_path2 = create_worktree('test-run', 'agent1', git_repo)
    assert worktree_path1 == worktree_path2
    assert (worktree_path2 / 'test_file.txt').read_text() == 'test content'

def test_create_worktree_from_base_ref(git_repo, monkeypatch):
    """Worktrees can be created from an explicit base ref."""
    _ = monkeypatch.chdir(git_repo)
    _ = subprocess.run(['git', 'checkout', '-b', 'base'], cwd=git_repo, check=True, capture_output=True)
    _ = (git_repo / 'base.txt').write_text('base')
    _ = subprocess.run(['git', 'add', 'base.txt'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Add base file'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'checkout', '-'], cwd=git_repo, check=True, capture_output=True)
    worktree_path = create_worktree('test-run-base', 'agent1', git_repo, base_ref='base')
    assert (worktree_path / 'base.txt').read_text() == 'base'

def test_run_worktree_setup_exposes_source_and_worktree_paths(git_repo, monkeypatch):
    """Setup commands should get explicit source/worktree path variables."""
    _ = monkeypatch.chdir(git_repo)
    worktree_path = create_worktree('test-run-setup', 'agent1', git_repo)
    _ = run_worktree_setup(worktree_path, git_repo, 'printf \'%s\n%s\n%s\n%s\n%s\n\' "$SPAWND_SOURCE_TREE_PATH" "$SPAWND_WORKTREE_PATH" "$WORKTREE_PRIMARY" "$CODEX_SOURCE_TREE_PATH" "$CODEX_WORKTREE_PATH" > setup.paths')
    lines = (worktree_path / 'setup.paths').read_text().splitlines()
    assert lines == [str(git_repo.resolve()), str(worktree_path.resolve()), str(git_repo.resolve()), str(git_repo.resolve()), str(worktree_path.resolve())]

def test_remove_worktree(git_repo, monkeypatch):
    """Test worktree removal."""
    _ = monkeypatch.chdir(git_repo)
    worktree_path = create_worktree('test-run', 'agent1', git_repo)
    assert worktree_path.exists()
    _ = remove_worktree(worktree_path, git_repo)
    assert not worktree_path.exists()

def test_merge_branch_success(git_repo, monkeypatch):
    """Test successful branch merge."""
    _ = monkeypatch.chdir(git_repo)
    worktree_path = create_worktree('test-run', 'feature', git_repo)
    _ = (worktree_path / 'feature.txt').write_text('feature content')
    _ = subprocess.run(['git', 'add', '.'], cwd=worktree_path, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Add feature'], cwd=worktree_path, check=True, capture_output=True)
    worktree2 = create_worktree('test-run', 'target', git_repo)
    result = merge_branch(worktree2, 'spawnd/test-run/feature', 'Merge feature')
    assert result is True
    assert (worktree2 / 'feature.txt').exists()

def test_run_git_command(git_repo):
    """Test run_git helper."""
    result = run_git(['status'], cwd=git_repo)
    assert result.returncode == 0
    assert 'nothing to commit' in result.stdout or 'clean' in result.stdout

def test_cleanup_run_worktrees_does_not_touch_prefix_sibling(git_repo, monkeypatch):
    """cleanup_run_worktrees must not remove worktrees from a run whose id
    merely shares a prefix with the target run."""
    from spawnd.gitops.worktrees import cleanup_run_worktrees
    _ = monkeypatch.chdir(git_repo)
    target_wt = create_worktree('abc', 'agent', git_repo)
    sibling_wt = create_worktree('abc-other', 'agent', git_repo)
    assert target_wt.exists()
    assert sibling_wt.exists()
    _ = cleanup_run_worktrees('abc', git_repo)
    assert not target_wt.exists()
    assert sibling_wt.exists()

def test_setup_worktree_with_deps_diff_only_propagates_deleted_files(git_repo, monkeypatch):
    """diff_only dependency imports should stage deletions, not leave stale files behind."""
    _ = monkeypatch.chdir(git_repo)
    _ = (git_repo / 'gone.txt').write_text('base')
    _ = subprocess.run(['git', 'add', 'gone.txt'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Add removable file'], cwd=git_repo, check=True, capture_output=True)
    dep = create_worktree('test-run', 'dep', git_repo)
    _ = subprocess.run(['git', 'rm', 'gone.txt'], cwd=dep, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Delete file'], cwd=dep, check=True, capture_output=True)
    child = create_worktree('test-run', 'child', git_repo)
    assert (child / 'gone.txt').exists()
    _ = setup_worktree_with_deps('test-run', 'child', ['dep'], child, mode='diff_only')
    assert not (child / 'gone.txt').exists()

def test_setup_worktree_with_deps_paths_propagates_deleted_files(git_repo, monkeypatch):
    """paths dependency imports should also stage deletions within matched paths."""
    _ = monkeypatch.chdir(git_repo)
    docs = git_repo / 'docs'
    _ = docs.mkdir()
    doomed = docs / 'gone.txt'
    _ = doomed.write_text('base')
    _ = subprocess.run(['git', 'add', 'docs/gone.txt'], cwd=git_repo, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Add docs file'], cwd=git_repo, check=True, capture_output=True)
    dep = create_worktree('test-run-paths', 'dep', git_repo)
    _ = subprocess.run(['git', 'rm', 'docs/gone.txt'], cwd=dep, check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', 'Delete docs file'], cwd=dep, check=True, capture_output=True)
    child = create_worktree('test-run-paths', 'child', git_repo)
    assert (child / 'docs' / 'gone.txt').exists()
    _ = setup_worktree_with_deps('test-run-paths', 'child', ['dep'], child, mode='paths', include_paths=['docs'])
    assert not (child / 'docs' / 'gone.txt').exists()
