"""Git worktree management for spawnd.dev."""
import logging
import os
import subprocess
from pathlib import Path
logger = logging.getLogger('spawnd.git')

class GitError(Exception):
    """Git operation failed."""
    pass

class WorktreeSetupError(GitError):
    """Worktree setup command failed."""
    pass


def get_worktrees_dir(run_id: str, repo_path: Path | None = None) -> Path:
    """Worker-local scratch directory for run worktrees."""

    repo = repo_path or Path.cwd()
    root = Path(os.environ.get('SPAWND_SCRATCH_ROOT', str(repo / '.spawnd-scratch')))
    return root / 'worktrees' / run_id

def run_git(args: list[str], cwd: Path | None=None, check: bool=True) -> subprocess.CompletedProcess:
    """Run a git command."""
    cmd = ['git'] + args
    _ = logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f'Git command failed: {result.stderr}')
    return result

def get_repo_root(cwd: Path | None=None) -> Path:
    """Get the root of the git repository."""
    result = run_git(['rev-parse', '--show-toplevel'], cwd=cwd)
    return Path(result.stdout.strip())

def get_default_branch(cwd: Path | None=None) -> str:
    """Detect the default branch (main or master)."""
    result = run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'], cwd=cwd, check=False)
    if result.returncode == 0:
        return result.stdout.strip().split('/')[-1]
    for branch in ['main', 'master']:
        result = run_git(['rev-parse', '--verify', branch], cwd=cwd, check=False)
        if result.returncode == 0:
            return branch
    return 'main'

def get_current_branch(cwd: Path | None=None) -> str:
    """Get current branch name."""
    result = run_git(['branch', '--show-current'], cwd=cwd)
    return result.stdout.strip()

def create_worktree(run_id: str, agent_name: str, repo_path: Path | None=None, base_ref: str | None=None, fetch: bool=False) -> Path:
    """Create a git worktree for an agent.

    If worktree already exists (resume scenario), returns existing path.

    Args:
        run_id: The run identifier
        agent_name: Name of the agent
        repo_path: Path to git repo (defaults to cwd)

    Returns:
        Path to the created or existing worktree
    """
    repo = repo_path or Path.cwd()
    worktree_path = get_worktrees_dir(run_id, repo) / agent_name
    branch_name = f'spawnd/{run_id}/{agent_name}'
    if worktree_path.exists() and (worktree_path / '.git').exists():
        _ = logger.info(f'Reusing existing worktree at {worktree_path}')
        return worktree_path
    _ = worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if fetch:
        _ = run_git(['fetch', '--prune', 'origin'], cwd=repo)
    args = ['worktree', 'add', '-b', branch_name, str(worktree_path)]
    if base_ref:
        _ = args.append(base_ref)
    _ = run_git(args, cwd=repo)
    _ = logger.info(f'Created worktree at {worktree_path} on branch {branch_name}')
    return worktree_path

def _tail(text: str, limit: int=4000) -> str:
    """Return the end of command output for diagnostics."""
    if len(text) <= limit:
        return text
    return text[-limit:]

def run_worktree_setup(worktree_path: Path, source_path: Path, command: str, *, env: dict[str, str] | None=None, timeout_seconds: int | None=None) -> subprocess.CompletedProcess:
    """Run a setup command in an agent worktree.

    The command receives explicit source/worktree paths so repository-owned
    bootstrap scripts can copy local-only files and verify setup without
    guessing linked-worktree internals.
    """
    setup_env = os.environ.copy()
    if env:
        _ = setup_env.update(env)
    source = str(source_path.resolve())
    worktree = str(worktree_path.resolve())
    _ = setup_env.update({'SPAWND_SOURCE_TREE_PATH': source, 'SPAWND_WORKTREE_PATH': worktree, 'WORKTREE_PRIMARY': source, 'CODEX_SOURCE_TREE_PATH': source, 'CODEX_WORKTREE_PATH': worktree})
    result = subprocess.run(command, cwd=worktree_path, env=setup_env, shell=True, capture_output=True, text=True, timeout=timeout_seconds)
    if result.returncode != 0:
        raise WorktreeSetupError(f'Worktree setup failed (exit {result.returncode}) for {worktree_path}: {command}\nstdout:\n{_tail(result.stdout)}\nstderr:\n{_tail(result.stderr)}')
    _ = logger.info(f'Ran worktree setup in {worktree_path}')
    return result

def remove_worktree(worktree_path: Path, repo_path: Path | None=None) -> None:
    """Remove a git worktree."""
    repo = repo_path or Path.cwd()
    _ = run_git(['worktree', 'remove', str(worktree_path), '--force'], cwd=repo)
    _ = logger.info(f'Removed worktree at {worktree_path}')

def delete_branch(branch: str, repo_path: Path | None=None) -> None:
    """Delete a git branch."""
    repo = repo_path or Path.cwd()
    _ = run_git(['branch', '-D', branch], cwd=repo)
    _ = logger.info(f'Deleted branch {branch}')

def list_worktrees(repo_path: Path | None=None) -> list[dict]:
    """List all worktrees."""
    repo = repo_path or Path.cwd()
    result = run_git(['worktree', 'list', '--porcelain'], cwd=repo)
    worktrees = []
    current = {}
    for line in result.stdout.strip().split('\n'):
        if not line:
            if current:
                _ = worktrees.append(current)
                current = {}
            continue
        if line.startswith('worktree '):
            current['path'] = line[9:]
        elif line.startswith('HEAD '):
            current['head'] = line[5:]
        elif line.startswith('branch '):
            current['branch'] = line[7:]
        elif line == 'bare':
            current['bare'] = True
        elif line == 'detached':
            current['detached'] = True
    if current:
        _ = worktrees.append(current)
    return worktrees

def merge_branch(worktree_path: Path, branch: str, message: str | None=None) -> bool:
    """Merge a branch into the worktree.

    Returns:
        True if merge succeeded, False if there were conflicts
    """
    msg = message or f'Merge {branch}'
    result = run_git(['merge', branch, '--no-edit', '-m', msg], cwd=worktree_path, check=False)
    if result.returncode != 0:
        if 'CONFLICT' in result.stdout or 'CONFLICT' in result.stderr:
            _ = logger.warning(f'Merge conflict merging {branch}')
            return False
        raise GitError(f'Merge failed: {result.stderr}')
    _ = logger.info(f'Merged {branch} into {worktree_path}')
    return True

def get_changed_files(branch: str, base: str, repo_path: Path | None=None) -> list[str]:
    """Get files changed between base and branch."""
    return [path for change in get_changed_file_changes(branch, base, repo_path) if (path := change['path'])]

def get_changed_file_changes(branch: str, base: str, repo_path: Path | None=None) -> list[dict[str, str | None]]:
    """Get path-level changes between base and branch.

    Returns dictionaries with:
    - ``status``: first letter of the git name-status code
    - ``path``: current path to apply in the dependent worktree
    - ``old_path``: previous path for renames/copies, else ``None``
    """
    repo = repo_path or Path.cwd()
    result = run_git(['diff', '--name-status', f'{base}...{branch}'], cwd=repo)
    changes = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        status = parts[0][:1]
        if status in {'R', 'C'} and len(parts) >= 3:
            _ = changes.append({'status': status, 'path': parts[2], 'old_path': parts[1]})
        elif len(parts) >= 2:
            _ = changes.append({'status': status, 'path': parts[1], 'old_path': None})
    return changes

def has_conflicts(worktree_path: Path) -> bool:
    """Check if worktree has unresolved conflicts."""
    result = run_git(['diff', '--name-only', '--diff-filter=U'], cwd=worktree_path, check=False)
    return bool(result.stdout.strip())

def commit(worktree_path: Path, message: str) -> None:
    """Create a commit in a worktree."""
    _ = run_git(['add', '-A'], cwd=worktree_path)
    result = run_git(['diff', '--cached', '--quiet'], cwd=worktree_path, check=False)
    if result.returncode != 0:
        _ = run_git(['commit', '-m', message], cwd=worktree_path)
        _ = logger.info(f'Committed in {worktree_path}: {message}')
    else:
        _ = logger.debug('Nothing to commit')

def _apply_dependency_change(worktree_path: Path, dep_branch: str, change: dict[str, str | None]) -> tuple[bool, str | None]:
    """Apply one dependency diff entry into the target worktree."""
    status = change['status']
    path = change['path']
    old_path = change['old_path']
    if not path:
        return (False, None)
    if status == 'D':
        result = run_git(['rm', '-f', '--ignore-unmatch', '--', path], cwd=worktree_path, check=False)
        if result.returncode != 0:
            return (False, path)
        return (True, None)
    if status == 'R' and old_path and (old_path != path):
        result = run_git(['rm', '-f', '--ignore-unmatch', '--', old_path], cwd=worktree_path, check=False)
        if result.returncode != 0:
            return (False, old_path)
    result = run_git(['checkout', dep_branch, '--', path], cwd=worktree_path, check=False)
    if result.returncode != 0:
        return (False, path)
    return (True, None)

def setup_worktree_with_deps(run_id: str, agent_name: str, depends_on: list[str], worktree_path: Path, mode: str='full', include_paths: list[str] | None=None, exclude_paths: list[str] | None=None) -> None:
    """Merge dependency branches into agent's worktree.

    Args:
        run_id: The run identifier
        agent_name: Name of the agent
        depends_on: List of dependency agent names
        worktree_path: Path to the worktree
        mode: "full", "diff_only", or "paths"
        include_paths: Paths to include (for mode="paths")
        exclude_paths: Paths to exclude
    """
    repo = get_repo_root(worktree_path)
    for dep_name in depends_on:
        dep_branch = f'spawnd/{run_id}/{dep_name}'
        if mode == 'full':
            if not merge_branch(worktree_path, dep_branch, f'Merge {dep_name} dependency'):
                raise GitError(f'Conflict merging dependency {dep_name}')
        elif mode == 'diff_only':
            base = get_default_branch(repo)
            changed_files = get_changed_file_changes(dep_branch, base, repo)
            checkout_failed = []
            applied_any = False
            for change in changed_files:
                applied, failed_path = _apply_dependency_change(worktree_path, dep_branch, change)
                if applied:
                    applied_any = True
                    continue
                if failed_path:
                    _ = checkout_failed.append(failed_path)
                    _ = logger.warning(f'Failed to apply {failed_path} from {dep_branch}')
            if checkout_failed:
                _ = logger.error(f'Failed to apply {len(checkout_failed)} files from {dep_name}')
            if applied_any:
                _ = commit(worktree_path, f'Import changes from {dep_name}')
        elif mode == 'paths':
            base = get_default_branch(repo)
            changed_files = get_changed_file_changes(dep_branch, base, repo)

            def matches_path(file: str, pattern: str) -> bool:
                """Check if file matches a path pattern (prefix or exact)."""
                pattern = pattern.rstrip('/')
                return file == pattern or file.startswith(pattern + '/')
            filtered_files = []
            for change in changed_files:
                candidates = [p for p in (change['path'], change['old_path']) if p]
                if include_paths:
                    included = any((matches_path(candidate, p) for candidate in candidates for p in include_paths))
                    if not included:
                        _ = logger.debug(f"Excluding {change['path']} - not in include_paths")
                        continue
                if exclude_paths:
                    excluded = any((matches_path(candidate, p) for candidate in candidates for p in exclude_paths))
                    if excluded:
                        _ = logger.debug(f"Excluding {change['path']} - matches exclude_paths")
                        continue
                _ = filtered_files.append(change)
            if filtered_files:
                _ = logger.info(f'Importing {len(filtered_files)} filtered files from {dep_name}')
                checkout_failed = []
                applied_any = False
                for change in filtered_files:
                    applied, failed_path = _apply_dependency_change(worktree_path, dep_branch, change)
                    if applied:
                        applied_any = True
                        continue
                    if failed_path:
                        _ = checkout_failed.append(failed_path)
                        _ = logger.warning(f'Failed to apply {failed_path} from {dep_branch}')
                if checkout_failed:
                    _ = logger.error(f'Failed to apply {len(checkout_failed)} files from {dep_name}')
                if applied_any:
                    _ = commit(worktree_path, f'Import filtered changes from {dep_name}')
            else:
                _ = logger.info(f'No files matched filters for {dep_name}')
    _ = logger.info(f'Setup worktree with deps: {depends_on}')

def cleanup_run_worktrees(run_id: str, repo_path: Path | None=None) -> None:
    """Clean up all worktrees and branches for a run."""
    repo = repo_path or Path.cwd()
    worktrees = list_worktrees(repo)
    expected_root = get_worktrees_dir(run_id, repo).resolve()
    for wt in worktrees:
        raw_path = wt.get('path')
        if not raw_path:
            continue
        try:
            wt_path = Path(raw_path).resolve()
        except OSError:
            continue
        if wt_path == expected_root or expected_root in wt_path.parents:
            _ = remove_worktree(Path(raw_path), repo)
    result = run_git(['branch', '--list', f'spawnd/{run_id}/*'], cwd=repo, check=False)
    for branch in result.stdout.strip().split('\n'):
        branch = branch.strip()
        if branch:
            _ = delete_branch(branch, repo)
    _ = logger.info(f'Cleaned up worktrees and branches for run {run_id}')
