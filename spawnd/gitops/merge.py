"""Branch merging utilities for spawnd.dev."""
import asyncio
import concurrent.futures
import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Literal
from spawnd.gitops.conflict_strategy import ConflictContext, strategy_for_mode
from spawnd.storage.db import get_agents, get_db, insert_agent, update_agent_status
from spawnd.core.deps import DependencyGraph
from spawnd.runtime.agent_run import AgentConfig
from spawnd.gitops.worktrees import merge_branch_to_current, remove_worktree
from spawnd.models.specs import AgentSpec
logger = logging.getLogger('spawnd.merge')

def _commit_resolved_conflicts(branch: str) -> None:
    _ = subprocess.run(['git', 'add', '.'], check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', f'Resolve conflicts from {branch}'], check=True, capture_output=True)

def _resolve_and_finalize(run_id: str, name: str, branch: str, conflict_files: list[str], cleanup: bool, agent: sqlite3.Row, resolver_timeout: int, resolver_max_cost: float, results: dict[str, list[Any]]) -> bool:
    """Resolve conflicts via resolver and finalize merge commit."""
    if not spawn_resolver(run_id, branch, conflict_files, resolver_timeout, resolver_max_cost):
        return False
    _ = _commit_resolved_conflicts(branch)
    _ = results['resolved'].append(name)
    _ = logger.info(f'Resolved and merged {name}')
    if cleanup:
        worktree = agent['worktree']
        if worktree:
            try:
                _ = remove_worktree(Path(worktree))
            except Exception as cleanup_err:
                _ = logger.warning(f'Failed to remove worktree {worktree}: {cleanup_err}')
    return True

def get_merge_order(run_id: str) -> list[str]:
    """Get the order for merging agent branches.

    Args:
        run_id: Run identifier

    Returns:
        List of agent names in merge order
    """
    with get_db(run_id) as db:
        agents = get_agents(db, run_id)
    completed = [a for a in agents if a['status'] == 'completed']
    if not completed:
        return []
    specs = [AgentSpec(name=a['name'], prompt=a['prompt'], depends_on=json.loads(a['depends_on']) if a['depends_on'] else []) for a in completed]
    graph = DependencyGraph(specs)
    return graph.topological_order()

def spawn_resolver(run_id: str, branch: str, conflict_files: list[str], timeout: int=120, max_cost: float=2.0) -> bool:
    """Spawn a resolver agent to fix merge conflicts.

    Args:
        run_id: Run identifier
        branch: Branch being merged
        conflict_files: List of files with conflicts
        timeout: Max time in seconds
        max_cost: Max cost in USD

    Returns:
        True if resolved successfully
    """
    if not conflict_files:
        _ = logger.warning('spawn_resolver called with empty conflict_files list')
        return False
    resolver_name = f"resolver-{branch.replace('/', '-')}"
    conflict_list = '\n'.join((f'- {f}' for f in conflict_files))
    prompt = f'Resolve merge conflicts in the following files:\n\n{conflict_list}\n\nThe current branch is being merged with {branch}.\nReview each conflict marker (<<<<<<< HEAD, =======, >>>>>>> {branch}) and resolve appropriately.\nAfter resolving, stage the files with git add.\n\nDo NOT run git merge --continue - that will be handled automatically.'
    with get_db(run_id) as db:
        _ = insert_agent(db, run_id, resolver_name, prompt, agent_type='worker', model='sonnet', max_iterations=10, max_cost_usd=max_cost)
    try:
        from spawnd.runtime.executor import run_worker
        config = AgentConfig(name=resolver_name, run_id=run_id, prompt=prompt, worktree=Path.cwd(), model='sonnet', max_iterations=10, max_cost_usd=max_cost)

        async def run_with_timeout():
            return await asyncio.wait_for(run_worker(config), timeout=timeout)
        try:
            _ = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, run_with_timeout()).result()
        except RuntimeError:
            result = asyncio.run(run_with_timeout())
        if result.get('success'):
            conflict_check = subprocess.run(['git', 'diff', '--check'], capture_output=True, text=True)
            if conflict_check.returncode == 0:
                _ = logger.info(f'Resolver {resolver_name} resolved conflicts')
                return True
            else:
                _ = logger.warning(f'Resolver {resolver_name} completed but conflicts remain')
                return False
        else:
            _ = logger.error(f"Resolver {resolver_name} failed: {result.get('error', 'unknown')}")
            return False
    except ImportError:
        _ = logger.warning('SDK not available for resolver, falling back to manual')
        return False
    except asyncio.TimeoutError:
        _ = logger.error(f'Resolver {resolver_name} timed out after {timeout}s')
        with get_db(run_id) as db:
            _ = update_agent_status(db, run_id, resolver_name, 'failed', 'timeout')
        return False
    except Exception as e:
        _ = logger.error(f'Resolver {resolver_name} error: {e}')
        try:
            with get_db(run_id) as db:
                _ = update_agent_status(db, run_id, resolver_name, 'failed', str(e)[:100])
        except Exception as db_err:
            _ = logger.warning(f'Failed to update resolver status in DB: {db_err}')
        return False

def get_conflict_files() -> list[str]:
    """Get list of files with merge conflicts."""
    result = subprocess.run(['git', 'diff', '--name-only', '--diff-filter=U'], capture_output=True, text=True)
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split('\n') if f]
    return []

def merge_run(run_id: str, cleanup: bool=True, on_conflict: Literal['spawn_resolver', 'fail', 'manual']='manual', resolver_timeout: int=120, resolver_max_cost: float=2.0) -> dict:
    """Merge all completed agent branches for a run.

    Args:
        run_id: Run identifier
        cleanup: Remove worktrees after merge
        on_conflict: How to handle conflicts (spawn_resolver, fail, manual)
        resolver_timeout: Timeout for resolver agent in seconds
        resolver_max_cost: Max cost for resolver agent in USD

    Returns:
        Dict with merge results
    """
    with get_db(run_id) as db:
        agents = get_agents(db, run_id)
    results = {'merged': [], 'failed': [], 'skipped': [], 'resolved': []}
    order = get_merge_order(run_id)
    agent_map = {a['name']: a for a in agents}
    strategy = strategy_for_mode(on_conflict)
    for name in order:
        maybe_agent = agent_map.get(name)
        if maybe_agent is None:
            _ = results['skipped'].append(name)
            continue
        agent: sqlite3.Row = maybe_agent
        branch = agent['branch']
        if not branch:
            _ = results['skipped'].append(name)
            continue
        try:
            merged = merge_branch_to_current(branch)
            if not merged:
                conflict_files = get_conflict_files()
                _ = logger.warning(f'Merge conflict for {name}: {conflict_files}')
                ctx = ConflictContext(name=name, branch=branch, conflict_files=conflict_files)
                should_continue, failure = strategy.handle(ctx, try_resolve=lambda: _resolve_and_finalize(run_id, name, branch, conflict_files, cleanup, agent, resolver_timeout, resolver_max_cost, results), abort_merge=lambda: subprocess.run(['git', 'merge', '--abort'], capture_output=True))
                if failure:
                    _ = results['failed'].append(failure)
                    if failure['error'] == 'conflict_manual':
                        _ = logger.warning(f'Merge conflict for {name}, leaving for manual resolution')
                        break
                    if failure['error'] == 'conflict':
                        _ = logger.error(f'Merge conflict for {name}, aborting (on_conflict=fail)')
                if should_continue:
                    continue
            _ = results['merged'].append(name)
            _ = logger.info(f'Merged {name} ({branch})')
            if cleanup:
                worktree = agent['worktree']
                if worktree:
                    try:
                        _ = remove_worktree(Path(worktree))
                    except Exception as e:
                        _ = logger.warning(f'Failed to remove worktree {worktree}: {e}')
        except Exception as e:
            conflict_files = get_conflict_files()
            if conflict_files:
                _ = logger.warning(f'Merge conflict for {name}: {conflict_files}')
                ctx = ConflictContext(name=name, branch=branch, conflict_files=conflict_files)
                should_continue, failure = strategy.handle(ctx, try_resolve=lambda: _resolve_and_finalize(run_id, name, branch, conflict_files, cleanup, agent, resolver_timeout, resolver_max_cost, results), abort_merge=lambda: subprocess.run(['git', 'merge', '--abort'], capture_output=True))
                if failure:
                    _ = results['failed'].append(failure)
                    if failure['error'] == 'conflict_manual':
                        _ = logger.warning(f'Merge conflict for {name}, leaving for manual resolution')
                        break
                    if failure['error'] == 'conflict':
                        _ = logger.error(f'Merge conflict for {name}, aborting (on_conflict=fail)')
                if should_continue:
                    continue
            else:
                _ = subprocess.run(['git', 'merge', '--abort'], capture_output=True)
                _ = results['failed'].append({'name': name, 'error': str(e)})
                _ = logger.error(f'Failed to merge {name}: {e}')
    return results

def check_conflicts(run_id: str) -> list[dict]:
    """Check for potential merge conflicts between agent branches.

    Args:
        run_id: Run identifier

    Returns:
        List of conflict info dicts
    """
    with get_db(run_id) as db:
        agents = get_agents(db, run_id)
    completed = [a for a in agents if a['status'] == 'completed' and a['branch']]
    conflicts = []
    for i, a1 in enumerate(completed):
        for a2 in completed[i + 1:]:
            try:
                result = subprocess.run(['git', 'merge-tree', 'HEAD', a1['branch'], a2['branch']], capture_output=True, text=True)
                if '<<<<<<' in result.stdout or '>>>>>>>' in result.stdout:
                    _ = conflicts.append({'agents': [a1['name'], a2['name']], 'branches': [a1['branch'], a2['branch']]})
            except Exception as e:
                _ = logger.warning(f"Failed to check conflicts between {a1['name']} and {a2['name']}: {e}")
    return conflicts

def squash_merge(branch: str, message: str | None=None) -> None:
    """Squash merge a branch into current branch.

    Args:
        branch: Branch name to merge
        message: Commit message (defaults to branch name)
    """
    msg = message or f'Squash merge {branch}'
    _ = subprocess.run(['git', 'merge', '--squash', branch], check=True, capture_output=True)
    _ = subprocess.run(['git', 'commit', '-m', msg], check=True, capture_output=True)

def interactive_merge(run_id: str) -> None:
    """Interactive merge with conflict resolution.

    Args:
        run_id: Run identifier
    """
    order = get_merge_order(run_id)
    with get_db(run_id) as db:
        agents = get_agents(db, run_id)
    agent_map = {a['name']: a for a in agents}
    for name in order:
        agent = agent_map.get(name)
        if not agent or not agent['branch']:
            continue
        branch = agent['branch']
        _ = print(f'\nMerging {name} ({branch})...')
        try:
            if not merge_branch_to_current(branch):
                raise subprocess.CalledProcessError(1, f'git merge {branch}')
            _ = print('  ✓ Merged successfully')
        except subprocess.CalledProcessError:
            _ = print('  ✗ Conflict detected')
            _ = print('    Resolve conflicts and run: git merge --continue')
            _ = print('    Or abort: git merge --abort')
            break
