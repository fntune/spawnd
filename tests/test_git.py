"""Tests for git module - worktree operations."""

import subprocess

import pytest

from spawnd.gitops.worktrees import (
    create_worktree,
    get_current_branch,
    merge_branch,
    merge_branch_to_current,
    remove_worktree,
    run_git,
    run_worktree_setup,
    setup_worktree_with_deps,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)

    # Create initial commit
    (repo / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    return repo


def test_get_current_branch(git_repo):
    """Test getting current branch name."""
    branch = get_current_branch(git_repo)
    assert branch in ("main", "master")


def test_create_worktree(git_repo, monkeypatch):
    """Test worktree creation."""
    monkeypatch.chdir(git_repo)

    worktree_path = create_worktree("test-run", "agent1", git_repo)

    assert worktree_path.exists()
    assert (worktree_path / ".git").exists()
    assert worktree_path.name == "agent1"


def test_create_worktree_reuse_existing(git_repo, monkeypatch):
    """Test that existing worktree is reused (resume scenario)."""
    monkeypatch.chdir(git_repo)

    # Create worktree first time
    worktree_path1 = create_worktree("test-run", "agent1", git_repo)

    # Create file in worktree
    (worktree_path1 / "test_file.txt").write_text("test content")

    # Create worktree second time (resume)
    worktree_path2 = create_worktree("test-run", "agent1", git_repo)

    # Should be same path and file should still exist
    assert worktree_path1 == worktree_path2
    assert (worktree_path2 / "test_file.txt").read_text() == "test content"


def test_create_worktree_from_base_ref(git_repo, monkeypatch):
    """Worktrees can be created from an explicit base ref."""
    monkeypatch.chdir(git_repo)

    subprocess.run(["git", "checkout", "-b", "base"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add base file"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-"], cwd=git_repo, check=True, capture_output=True)

    worktree_path = create_worktree("test-run-base", "agent1", git_repo, base_ref="base")

    assert (worktree_path / "base.txt").read_text() == "base"


def test_run_worktree_setup_exposes_source_and_worktree_paths(git_repo, monkeypatch):
    """Setup commands should get explicit source/worktree path variables."""
    monkeypatch.chdir(git_repo)

    worktree_path = create_worktree("test-run-setup", "agent1", git_repo)
    run_worktree_setup(
        worktree_path,
        git_repo,
        "printf '%s\n%s\n%s\n%s\n%s\n' "
        '"$SPAWND_SOURCE_TREE_PATH" "$SPAWND_WORKTREE_PATH" "$WORKTREE_PRIMARY" '
        '"$CODEX_SOURCE_TREE_PATH" "$CODEX_WORKTREE_PATH" > setup.paths',
    )

    lines = (worktree_path / "setup.paths").read_text().splitlines()
    assert lines == [
        str(git_repo.resolve()),
        str(worktree_path.resolve()),
        str(git_repo.resolve()),
        str(git_repo.resolve()),
        str(worktree_path.resolve()),
    ]


def test_remove_worktree(git_repo, monkeypatch):
    """Test worktree removal."""
    monkeypatch.chdir(git_repo)

    worktree_path = create_worktree("test-run", "agent1", git_repo)
    assert worktree_path.exists()

    remove_worktree(worktree_path, git_repo)
    assert not worktree_path.exists()


def test_merge_branch_success(git_repo, monkeypatch):
    """Test successful branch merge."""
    monkeypatch.chdir(git_repo)

    # Create worktree with new branch
    worktree_path = create_worktree("test-run", "feature", git_repo)

    # Make a change in the worktree
    (worktree_path / "feature.txt").write_text("feature content")
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=worktree_path, check=True, capture_output=True)

    # Create another worktree to merge into
    worktree2 = create_worktree("test-run", "target", git_repo)

    # Merge the feature branch into target worktree
    result = merge_branch(worktree2, "spawnd/test-run/feature", "Merge feature")

    assert result is True
    assert (worktree2 / "feature.txt").exists()


def test_merge_branch_to_current(git_repo, monkeypatch):
    """Test merge into current branch."""
    monkeypatch.chdir(git_repo)

    # Create a feature branch
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "feature.txt").write_text("feature content")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=git_repo, check=True, capture_output=True)

    # Switch back to main
    subprocess.run(["git", "checkout", "-"], cwd=git_repo, check=True, capture_output=True)

    # Merge feature into current
    result = merge_branch_to_current("feature", repo_path=git_repo)

    assert result is True
    assert (git_repo / "feature.txt").exists()


def test_run_git_command(git_repo):
    """Test run_git helper."""
    result = run_git(["status"], cwd=git_repo)
    assert result.returncode == 0
    assert "nothing to commit" in result.stdout or "clean" in result.stdout


def test_cleanup_run_worktrees_does_not_touch_prefix_sibling(git_repo, monkeypatch):
    """cleanup_run_worktrees must not remove worktrees from a run whose id
    merely shares a prefix with the target run."""
    from spawnd.gitops.worktrees import cleanup_run_worktrees

    monkeypatch.chdir(git_repo)

    # Two runs where one id is a prefix of the other.
    target_wt = create_worktree("abc", "agent", git_repo)
    sibling_wt = create_worktree("abc-other", "agent", git_repo)

    assert target_wt.exists()
    assert sibling_wt.exists()

    cleanup_run_worktrees("abc", git_repo)

    # Target run's worktree is gone, sibling's is untouched.
    assert not target_wt.exists()
    assert sibling_wt.exists()


def test_setup_worktree_with_deps_diff_only_propagates_deleted_files(git_repo, monkeypatch):
    """diff_only dependency imports should stage deletions, not leave stale files behind."""
    monkeypatch.chdir(git_repo)

    (git_repo / "gone.txt").write_text("base")
    subprocess.run(["git", "add", "gone.txt"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add removable file"], cwd=git_repo, check=True, capture_output=True)

    dep = create_worktree("test-run", "dep", git_repo)
    subprocess.run(["git", "rm", "gone.txt"], cwd=dep, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Delete file"], cwd=dep, check=True, capture_output=True)

    child = create_worktree("test-run", "child", git_repo)
    assert (child / "gone.txt").exists()

    setup_worktree_with_deps("test-run", "child", ["dep"], child, mode="diff_only")

    assert not (child / "gone.txt").exists()


def test_setup_worktree_with_deps_paths_propagates_deleted_files(git_repo, monkeypatch):
    """paths dependency imports should also stage deletions within matched paths."""
    monkeypatch.chdir(git_repo)

    docs = git_repo / "docs"
    docs.mkdir()
    doomed = docs / "gone.txt"
    doomed.write_text("base")
    subprocess.run(["git", "add", "docs/gone.txt"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add docs file"], cwd=git_repo, check=True, capture_output=True)

    dep = create_worktree("test-run-paths", "dep", git_repo)
    subprocess.run(["git", "rm", "docs/gone.txt"], cwd=dep, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Delete docs file"], cwd=dep, check=True, capture_output=True)

    child = create_worktree("test-run-paths", "child", git_repo)
    assert (child / "docs" / "gone.txt").exists()

    setup_worktree_with_deps(
        "test-run-paths",
        "child",
        ["dep"],
        child,
        mode="paths",
        include_paths=["docs"],
    )

    assert not (child / "docs" / "gone.txt").exists()
