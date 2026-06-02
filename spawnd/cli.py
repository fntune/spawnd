"""CLI for spawnd.dev."""
import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
import click
import yaml
from spawnd.storage.db import get_agents, get_db, get_plan, get_total_cost, list_runs, open_db, update_agent_status, update_plan_status
from spawnd.storage.paths import get_db_path, get_run_dir
from spawnd.core.deps import DependencyGraph
from spawnd.gitops.worktrees import cleanup_run_worktrees, merge_branch_to_current
from spawnd.storage.logs import list_logs, read_all_logs, read_log, setup_logging, tail_log
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
from spawnd.io.parser import generate_run_id, parse_plan_file
from spawnd.io.validation import validate_plan
from spawnd.io.plan_builder import create_inline_plan
from spawnd.roles import BUILTIN_ROLES, get_role
from spawnd.runtime.scheduler import run_plan
from spawnd.runtime.run_state import run_has_persisted_plan
logger = logging.getLogger('spawnd.cli')

def ensure_run_exists(run_id: str) -> None:
    """Abort with a friendly message when a run does not exist."""
    if not run_has_persisted_plan(run_id):
        _ = click.echo(f'Run not found: {run_id}', err=True)
        raise click.Abort()

@click.group()
@click.version_option()
def main() -> None:
    """spawnd.dev — multi-agent orchestration."""
    pass

@main.command()
@click.option('-f', '--file', 'plan_file', type=click.Path(exists=True), help='Plan YAML file')
@click.option('-p', '--prompt', multiple=True, help='Inline agent prompts')
@click.option('--check', 'check_cmd', default=None, help='Check command for inline prompts')
@click.option('--sequential', is_flag=True, help='Run agents sequentially')
@click.option('--run-id', 'run_id', default=None, help='Explicit run ID')
@click.option('--resume', is_flag=True, help='Resume existing run')
@click.option('--mock', is_flag=True, help='Use mock workers (for testing)')
@click.option('-v', '--verbose', is_flag=True, help='Verbose output')
def run(plan_file: str | None, prompt: tuple[str, ...], check_cmd: str | None, sequential: bool, run_id: str | None, resume: bool, mock: bool, verbose: bool) -> None:
    """Run a spawnd plan."""
    if resume and (not run_id):
        raise click.UsageError('--resume requires --run-id')
    if resume and run_id:
        _ = ensure_run_exists(run_id)
    if resume and run_id and run_has_persisted_plan(run_id):
        if plan_file:
            plan = parse_plan_file(Path(plan_file))
        elif prompt:
            defaults = Defaults(check=check_cmd) if check_cmd else None
            plan = create_inline_plan(list(prompt), sequential=sequential, defaults=defaults)
        else:
            with get_db(run_id) as db:
                plan_row = get_plan(db, run_id)
            if not plan_row:
                raise click.UsageError(f'Plan not found for run: {run_id}')
            plan = PlanSpec(**yaml.safe_load(plan_row['spec']))
    elif plan_file:
        plan = parse_plan_file(Path(plan_file))
    elif prompt:
        defaults = Defaults(check=check_cmd) if check_cmd else None
        plan = create_inline_plan(list(prompt), sequential=sequential, defaults=defaults)
    else:
        raise click.UsageError('Either --file or --prompt is required')
    if not resume:
        errors = validate_plan(plan)
        if errors:
            for error in errors:
                _ = click.echo(f'Error: {error}', err=True)
            raise click.Abort()
    actual_run_id = run_id or generate_run_id(plan.name)
    _ = setup_logging(actual_run_id, verbose)
    if resume:
        _ = click.echo(f'Resuming run: {actual_run_id}')
    else:
        _ = click.echo(f'Starting run: {actual_run_id}')
    _ = click.echo(f'Agents: {[a.name for a in plan.agents]}')
    result = asyncio.run(run_plan(plan, actual_run_id, use_mock=mock, resume=resume))
    _ = click.echo(f'\nRun completed: {result.run_id}')
    _ = click.echo(f'Success: {result.success}')
    _ = click.echo(f'Completed: {result.completed}')
    _ = click.echo(f'Failed: {result.failed}')
    _ = click.echo(f'Total cost: ${result.total_cost:.4f}')

@main.command()
@click.argument('run_id', required=False)
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
def status(run_id: str | None, as_json: bool) -> None:
    """Show status of a run. If no run_id, shows latest."""
    if not run_id:
        run_id = next((candidate for candidate in list_runs() if run_has_persisted_plan(candidate)), None)
        if not run_id:
            _ = click.echo('No runs found', err=True)
            raise click.Abort()
    else:
        _ = ensure_run_exists(run_id)
    with get_db(run_id) as db:
        plan = get_plan(db, run_id)
        if not plan:
            _ = click.echo(f'Plan not found for run: {run_id}', err=True)
            raise click.Abort()
        agents = get_agents(db, run_id)
        total_cost = get_total_cost(db, run_id)
        if as_json:
            output = {'run_id': run_id, 'plan': plan['name'], 'status': plan['status'], 'total_cost': total_cost, 'agents': [{'name': a['name'], 'status': a['status'], 'type': a['type'], 'iteration': a['iteration'], 'max_iterations': a['max_iterations'], 'cost': a['cost_usd'], 'error': a['error']} for a in agents]}
            _ = click.echo(json.dumps(output, indent=2))
        else:
            _ = click.echo(f'Run: {run_id}')
            _ = click.echo(f"Plan: {plan['name']}")
            _ = click.echo(f"Status: {plan['status']}")
            _ = click.echo(f'Cost: ${total_cost:.4f}')
            _ = click.echo('\nAgents:')
            for agent in agents:
                status_str = agent['status']
                if agent['error']:
                    status_str += f" ({agent['error'][:50]})"
                _ = click.echo(f"  {agent['name']}: {status_str}")

@main.command()
@click.argument('run_id')
@click.option('-a', '--agent', help='Specific agent name')
@click.option('-n', '--lines', type=int, help='Number of lines')
@click.option('-f', '--follow', is_flag=True, help='Follow log output')
@click.option('--all', 'show_all', is_flag=True, help='Show all agent logs')
def logs(run_id: str, agent: str | None, lines: int | None, follow: bool, show_all: bool) -> None:
    """View logs for a run."""
    if show_all:
        content = read_all_logs(run_id)
        _ = click.echo(content)
    elif agent:
        if follow:
            _ = tail_log(run_id, agent, follow=True)
        else:
            content = read_log(run_id, agent, lines=lines)
            _ = click.echo(content)
    else:
        available = list_logs(run_id)
        if available:
            _ = click.echo(f'Available logs for {run_id}:')
            for name in sorted(available):
                _ = click.echo(f'  {name}')
        else:
            _ = click.echo(f'No logs found for {run_id}')

@main.command()
@click.argument('run_id')
def cancel(run_id: str) -> None:
    """Cancel a running spawnd run."""
    _ = ensure_run_exists(run_id)
    with get_db(run_id) as db:
        _ = update_plan_status(db, run_id, 'cancelled')
        agents = get_agents(db, run_id)
        cancelled = 0
        for agent in agents:
            if agent['status'] not in ('completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'):
                _ = update_agent_status(db, run_id, agent['name'], 'cancelled')
                cancelled += 1
        _ = click.echo(f'Cancelled run {run_id}')
        _ = click.echo(f'Agents cancelled: {cancelled}')

@main.command()
@click.argument('run_id')
@click.option('--dry-run', is_flag=True, help='Show what would be merged')
def merge(run_id: str, dry_run: bool) -> None:
    """Merge completed agent branches."""
    _ = ensure_run_exists(run_id)
    with get_db(run_id) as db:
        agents = get_agents(db, run_id)
        completed = [a for a in agents if a['status'] == 'completed']
        if not completed:
            _ = click.echo('No completed agents to merge')
            return
        specs = [AgentSpec(name=a['name'], prompt=a['prompt'], depends_on=json.loads(a['depends_on']) if a['depends_on'] else []) for a in completed]
        graph = DependencyGraph(specs)
        try:
            merge_order = graph.topological_order()
        except ValueError as e:
            _ = click.echo(f'Error: {e}', err=True)
            raise click.Abort()
        _ = click.echo(f'Merge order: {merge_order}')
        if dry_run:
            _ = click.echo('(dry run - no changes made)')
            return
        for name in merge_order:
            agent = next((a for a in completed if a['name'] == name))
            branch = agent['branch']
            if branch:
                _ = click.echo(f'Merging {name} ({branch})...')
                try:
                    merged = merge_branch_to_current(branch)
                except Exception as e:
                    _ = click.echo(f'  Failed: {e}', err=True)
                    raise click.Abort()
                if not merged:
                    _ = click.echo('  Merge conflict detected. Resolve it manually before continuing.', err=True)
                    raise click.Abort()
                _ = click.echo('  Merged successfully')

@main.command()
@click.argument('run_id')
def dashboard(run_id: str) -> None:
    """Show live dashboard for a run."""
    _ = ensure_run_exists(run_id)
    db = open_db(run_id)
    try:
        while True:
            _ = click.clear()
            plan = get_plan(db, run_id)
            agents = get_agents(db, run_id)
            cost = get_total_cost(db, run_id)
            _ = click.echo(f'=== {run_id} ===')
            _ = click.echo(f"Status: {(plan['status'] if plan else 'unknown')}")
            _ = click.echo(f'Cost: ${cost:.4f}')
            _ = click.echo()
            counts = {}
            for a in agents:
                counts[a['status']] = counts.get(a['status'], 0) + 1
            _ = click.echo(f'Agents: {counts}')
            _ = click.echo()
            for agent in agents:
                icon = {'pending': '⏳', 'running': '🔄', 'completed': '✅', 'failed': '❌', 'cancelled': '🚫', 'paused': '⏸️', 'timeout': '⌛', 'cost_exceeded': '💸'}.get(agent['status'], '?')
                _ = click.echo(f"{icon} {agent['name']}: {agent['status']}")
            all_done = all((a['status'] in ('completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded', 'paused') for a in agents))
            if all_done:
                _ = click.echo('\nAll agents finished.')
                break
            _ = time.sleep(2)
    except KeyboardInterrupt:
        _ = click.echo('\n')
    finally:
        _ = db.close()

@main.command()
@click.argument('run_id')
@click.option('-v', '--verbose', is_flag=True, help='Verbose output')
def resume(run_id: str, verbose: bool) -> None:
    """Resume a previous run (alias for run --resume --run-id)."""
    _ = ensure_run_exists(run_id)
    with get_db(run_id) as db:
        plan_row = get_plan(db, run_id)
    if not plan_row:
        _ = click.echo(f'Plan not found for run: {run_id}', err=True)
        raise click.Abort()
    plan = PlanSpec(**yaml.safe_load(plan_row['spec']))
    _ = setup_logging(run_id, verbose)
    _ = click.echo(f'Resuming run: {run_id}')
    _ = click.echo(f'Agents: {[a.name for a in plan.agents]}')
    result = asyncio.run(run_plan(plan, run_id, resume=True))
    _ = click.echo(f'\nRun completed: {result.run_id}')
    _ = click.echo(f'Success: {result.success}')
    _ = click.echo(f'Completed: {result.completed}')
    _ = click.echo(f'Failed: {result.failed}')
    _ = click.echo(f'Total cost: ${result.total_cost:.4f}')

@main.command()
@click.argument('run_id', required=False)
@click.option('--all', 'clean_all', is_flag=True, help='Clean all runs')
def clean(run_id: str | None, clean_all: bool) -> None:
    """Clean up run artifacts (worktrees, db)."""
    if clean_all:
        runs = list_runs()
        if not runs:
            _ = click.echo('No runs found')
            return
        for rid in runs:
            run_dir = get_run_dir(rid)
            if run_dir.exists():
                try:
                    _ = cleanup_run_worktrees(rid)
                except Exception as e:
                    _ = logger.warning(f'Failed to clean git worktrees for {rid}: {e}')
                _ = shutil.rmtree(run_dir)
                _ = click.echo(f'Cleaned: {rid}')
        _ = click.echo(f'Cleaned {len(runs)} runs')
    elif run_id:
        run_dir = get_run_dir(run_id)
        if run_dir.exists():
            try:
                _ = cleanup_run_worktrees(run_id)
            except Exception as e:
                _ = logger.warning(f'Failed to clean git worktrees for {run_id}: {e}')
            _ = shutil.rmtree(run_dir)
            _ = click.echo(f'Cleaned: {run_id}')
        else:
            _ = click.echo(f'Run not found: {run_id}', err=True)
    else:
        raise click.UsageError('Provide a run_id or use --all')

@main.command()
@click.argument('run_id', required=False)
@click.argument('query', required=False)
def db(run_id: str | None, query: str | None) -> None:
    """Query the SQLite database."""
    if not run_id:
        runs = list_runs()
        if runs:
            _ = click.echo('Available runs:')
            for rid in runs[:10]:
                _ = click.echo(f'  {rid}')
            if len(runs) > 10:
                _ = click.echo(f'  ... and {len(runs) - 10} more')
        else:
            _ = click.echo('No runs found')
        return
    _ = ensure_run_exists(run_id)
    with get_db(run_id) as conn:
        if query:
            try:
                cursor = conn.execute(query)
                rows = cursor.fetchall()
                if rows:
                    cols = [desc[0] for desc in cursor.description]
                    _ = click.echo('\t'.join(cols))
                    _ = click.echo('-' * 60)
                    for row in rows:
                        _ = click.echo('\t'.join((str(v) for v in row)))
                else:
                    _ = click.echo('No results')
            except Exception as e:
                _ = click.echo(f'Query error: {e}', err=True)
        else:
            _ = click.echo(f'Database: {get_db_path(run_id)}')
            _ = click.echo('Usage: spawnd db <run_id> "SELECT * FROM agents"')
            _ = click.echo('\nTables: plans, agents, events, responses')

@main.command()
@click.argument('role_name', required=False)
def roles(role_name: str | None) -> None:
    """List available roles or show role details."""
    if role_name:
        role = get_role(role_name)
        if not role:
            _ = click.echo(f'Role not found: {role_name}', err=True)
            _ = click.echo(f"Available: {', '.join(BUILTIN_ROLES.keys())}")
            raise click.Abort()
        _ = click.echo(f'Role: {role.name}')
        _ = click.echo(f'Description: {role.description}')
        if role.model:
            _ = click.echo(f'Default model: {role.model}')
        if role.check:
            _ = click.echo(f'Default check: {role.check}')
        _ = click.echo(f'\nSystem prompt:\n{role.system_prompt}')
    else:
        _ = click.echo('Available roles:')
        for name, role in BUILTIN_ROLES.items():
            _ = click.echo(f'  {name}: {role.description}')
if __name__ == '__main__':
    _ = main()
