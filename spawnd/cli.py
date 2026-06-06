"""Deployed-only CLI for spawnd.dev."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from spawnd.artifacts.store import InMemoryArtifactStore, S3ArtifactStore
from spawnd.config import load_backend_config
from spawnd.coordination.redis import RedisCoordinator
from spawnd.io.parser import generate_run_id, parse_plan_file
from spawnd.io.plan_builder import create_inline_plan
from spawnd.io.validation import validate_plan
from spawnd.models.specs import Defaults, PlanSpec
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.roles import BUILTIN_ROLES, get_role
from spawnd.state.repository import DeployedRepository
from spawnd.state.submission import submit_plan, worker_id as make_worker_id
from spawnd.workers.worker import DeployedWorker, reconcile_ready_agents


def _config():
    return load_backend_config()


def _repository(*, create_schema: bool = False) -> DeployedRepository:
    config = _config()
    if not config.database_url:
        raise click.UsageError("SPAWND_DATABASE_URL is required")
    repo = DeployedRepository.from_url(config.database_url)
    if create_schema:
        repo.create_schema()
    return repo


def _coordinator() -> RedisCoordinator:
    config = _config()
    if not config.redis_url:
        raise click.UsageError("SPAWND_REDIS_URL is required")
    return RedisCoordinator.from_url(config.redis_url)


def _artifact_store() -> S3ArtifactStore:
    config = _config()
    if not config.artifacts.configured:
        raise click.UsageError("SPAWND_ARTIFACTS_BUCKET is required to read artifact-backed logs")
    return S3ArtifactStore(config.artifacts)


def _components(*, create_schema: bool = False, use_memory_artifacts: bool = False):
    config = _config()
    repo = _repository(create_schema=create_schema)
    coordinator = _coordinator()
    if config.artifacts.configured:
        artifacts = S3ArtifactStore(config.artifacts)
    elif use_memory_artifacts:
        artifacts = InMemoryArtifactStore()
    else:
        raise click.UsageError("SPAWND_ARTIFACTS_BUCKET is required; --in-memory-artifacts is for tests")
    telemetry = TelemetryRecorder(config.telemetry, repo)
    return repo, coordinator, artifacts, telemetry


def _load_plan(plan_file: str | None, prompt: tuple[str, ...], check_cmd: str | None, sequential: bool) -> PlanSpec:
    if plan_file:
        return parse_plan_file(Path(plan_file))
    if prompt:
        defaults = Defaults(check=check_cmd) if check_cmd else None
        return create_inline_plan(list(prompt), sequential=sequential, defaults=defaults)
    raise click.UsageError("Either --file or --prompt is required")


def _validate(plan: PlanSpec) -> None:
    errors = validate_plan(plan)
    if not errors:
        return
    for error in errors:
        click.echo(f"Error: {error}", err=True)
    raise click.Abort()


def _submit(
    plan: PlanSpec,
    *,
    run_id: str | None,
    source_repo: str | None,
    source_ref: str | None,
    create_schema: bool,
) -> str:
    _validate(plan)
    repo = _repository(create_schema=create_schema)
    coordinator = _coordinator()
    actual_run_id = submit_plan(
        plan,
        repository=repo,
        coordinator=coordinator,
        run_id=run_id,
        source_repo=source_repo or str(Path.cwd()),
        source_ref=source_ref,
    )
    return actual_run_id


def _json_echo(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, default=str))


@click.group()
@click.version_option()
def main() -> None:
    """spawnd.dev deployed orchestration."""


@main.command()
@click.option("-f", "--file", "plan_file", type=click.Path(exists=True), help="Plan YAML file")
@click.option("-p", "--prompt", multiple=True, help="Inline agent prompts")
@click.option("--check", "check_cmd", default=None, help="Check command for inline prompts")
@click.option("--sequential", is_flag=True, help="Run inline agents sequentially")
@click.option("--run-id", default=None, help="Explicit run id")
@click.option("--source-repo", default=None, help="Source repository path")
@click.option("--source-ref", default=None, help="Source revision/ref")
@click.option("--create-schema", is_flag=True, help="Create deployed schema before submit")
def run(
    plan_file: str | None,
    prompt: tuple[str, ...],
    check_cmd: str | None,
    sequential: bool,
    run_id: str | None,
    source_repo: str | None,
    source_ref: str | None,
    create_schema: bool,
) -> None:
    """Submit a run to the deployed backend."""

    plan = _load_plan(plan_file, prompt, check_cmd, sequential)
    actual_run_id = _submit(
        plan,
        run_id=run_id or (plan.run.id if plan.run and plan.run.id else generate_run_id(plan.name)),
        source_repo=source_repo,
        source_ref=source_ref,
        create_schema=create_schema,
    )
    click.echo(f"Submitted run: {actual_run_id}")


@main.command()
@click.option("-f", "--file", "plan_file", type=click.Path(exists=True), required=True, help="Plan YAML file")
@click.option("--run-id", default=None, help="Explicit run id")
@click.option("--source-repo", default=None, help="Source repository path")
@click.option("--source-ref", default=None, help="Source revision/ref")
@click.option("--create-schema", is_flag=True, help="Create deployed schema before submit")
def submit(plan_file: str, run_id: str | None, source_repo: str | None, source_ref: str | None, create_schema: bool) -> None:
    """Alias for deployed plan submission."""

    actual_run_id = _submit(
        parse_plan_file(Path(plan_file)),
        run_id=run_id,
        source_repo=source_repo,
        source_ref=source_ref,
        create_schema=create_schema,
    )
    click.echo(f"Submitted run: {actual_run_id}")


@main.command()
@click.option("--once", "run_once_flag", is_flag=True, help="Claim and execute at most one ready agent")
@click.option("--poll", "run_poll_flag", is_flag=True, help="Continuously poll and execute ready agents")
@click.option("--mock", is_flag=True, help="Use the fake runtime executor")
@click.option("--worker-id", "worker_id_value", default=None, help="Stable worker id")
@click.option("--source-path", type=click.Path(exists=True, file_okay=False), default=None, help="Source repository path")
@click.option("--lease-seconds", type=int, default=300, show_default=True)
@click.option("--block-ms", type=int, default=1000, show_default=True)
@click.option("--idle-sleep-seconds", type=float, default=1.0, show_default=True)
@click.option("--create-schema", is_flag=True, help="Create deployed schema before starting")
@click.option("--in-memory-artifacts", is_flag=True, help="Use volatile test artifacts")
def worker(
    run_once_flag: bool,
    run_poll_flag: bool,
    mock: bool,
    worker_id_value: str | None,
    source_path: str | None,
    lease_seconds: int,
    block_ms: int,
    idle_sleep_seconds: float,
    create_schema: bool,
    in_memory_artifacts: bool,
) -> None:
    """Run a deployed worker against Postgres and Redis."""

    if run_once_flag == run_poll_flag:
        raise click.UsageError("Choose exactly one of --once or --poll")
    repo, coordinator, artifacts, telemetry = _components(create_schema=create_schema, use_memory_artifacts=in_memory_artifacts)
    deployed_worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=artifacts,
        telemetry=telemetry,
        worker_id=worker_id_value or make_worker_id(),
        source_path=Path(source_path) if source_path else None,
        lease_seconds=lease_seconds,
        use_mock=mock,
    )
    if run_once_flag:
        result = asyncio.run(deployed_worker.run_once(block_ms=block_ms))
        if result.claimed:
            click.echo(f"Worker {deployed_worker.worker_id} finished {result.run_id}/{result.agent}: {result.status}")
        else:
            click.echo(f"Worker {deployed_worker.worker_id} found no ready agents")
        return
    click.echo(f"Worker {deployed_worker.worker_id} polling")
    asyncio.run(deployed_worker.run_poll(idle_sleep_seconds=idle_sleep_seconds, block_ms=block_ms))


@main.command()
@click.option("--create-schema", is_flag=True, help="Create deployed schema before reconciling")
def reconcile(create_schema: bool) -> None:
    """Recover Redis queue hints from canonical Postgres state."""

    repo = _repository(create_schema=create_schema)
    coordinator = _coordinator()
    requeued = reconcile_ready_agents(repo, coordinator)
    click.echo(f"Requeued hints: {len(requeued)}")
    for item in requeued:
        click.echo(f"  {item['run_id']}/{item['agent']}")


@main.command("worker-heartbeat")
@click.option("--worker-id", "worker_id_value", default=None, help="Stable worker id")
@click.option("--create-schema", is_flag=True, help="Create deployed schema first")
def worker_heartbeat(worker_id_value: str | None, create_schema: bool) -> None:
    """Record a worker heartbeat."""

    repo = _repository(create_schema=create_schema)
    worker_id_value = worker_id_value or make_worker_id()
    repo.record_worker_heartbeat(worker_id_value)
    _coordinator().heartbeat(worker_id_value)
    click.echo(f"Heartbeat recorded: {worker_id_value}")


@main.command()
@click.argument("run_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def status(run_id: str | None, as_json: bool) -> None:
    """Show deployed run status."""

    repo = _repository()
    if run_id is None:
        rows = repo.list_runs(limit=1)
        if not rows:
            click.echo("No runs found", err=True)
            raise click.Abort()
        run_id = str(rows[0]["run_id"])
    run_row = repo.get_run(run_id)
    if run_row is None:
        raise click.UsageError(f"Run not found: {run_id}")
    agents = repo.get_agents(run_id)
    payload = {
        "run_id": run_id,
        "plan": run_row["name"],
        "status": run_row["status"],
        "total_cost": run_row["total_cost_usd"],
        "telemetry": repo.telemetry_summary(run_id),
        "agents": agents,
    }
    if as_json:
        _json_echo(payload)
        return
    click.echo(f"Run: {run_id}")
    click.echo(f"Plan: {run_row['name']}")
    click.echo(f"Status: {run_row['status']}")
    click.echo(f"Cost: ${float(run_row['total_cost_usd'] or 0.0):.4f}")
    click.echo("\nAgents:")
    for agent in agents:
        detail = f" ({agent['error'][:80]})" if agent.get("error") else ""
        click.echo(f"  {agent['name']}: {agent['status']}{detail}")


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.option("--limit", type=int, default=100, show_default=True)
def events(run_id: str, agent_name: str | None, as_json: bool, limit: int) -> None:
    """List deployed run events."""

    rows = _repository().get_events(run_id, limit=limit)
    if agent_name:
        rows = [row for row in rows if row["agent"] == agent_name]
    if as_json:
        _json_echo(rows)
        return
    for row in rows:
        click.echo(f"{row.get('created_at')} {row['agent']} {row['event_type']}")


@main.command("live-events")
@click.argument("run_id")
@click.option("--interval", type=float, default=2.0, show_default=True)
def live_events(run_id: str, interval: float) -> None:
    """Poll deployed events as a simple live status view."""

    repo = _repository()
    seen: set[str] = set()
    while True:
        for row in reversed(repo.get_events(run_id, limit=100)):
            event_id = str(row["id"])
            if event_id in seen:
                continue
            seen.add(event_id)
            click.echo(f"{row.get('created_at')} {row['agent']} {row['event_type']}")
        time.sleep(interval)


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def artifacts(run_id: str, agent_name: str | None, as_json: bool) -> None:
    """List deployed artifact metadata."""

    rows = _repository().get_artifacts(run_id, agent_name)
    if as_json:
        _json_echo(rows)
        return
    for row in rows:
        label = row.get("agent") or "_system"
        click.echo(f"{row.get('created_at')} {label} {row['kind']} {row['uri']}")


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.option("--metadata", is_flag=True, help="Only show artifact metadata")
def logs(run_id: str, agent_name: str | None, as_json: bool, metadata: bool) -> None:
    """Read artifact-backed runtime output."""

    rows = [
        row
        for row in _repository().get_artifacts(run_id, agent_name)
        if row["kind"] in {"runtime-output", "final-message", "setup-output", "check-output", "runtime-error"}
    ]
    if as_json or metadata:
        _json_echo(rows)
        return
    store = _artifact_store()
    for row in rows:
        label = row.get("agent") or "_system"
        click.echo(f"== {row.get('created_at')} {label} {row['kind']} ==")
        click.echo(store.get_text(str(row["uri"])).rstrip())


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def checks(run_id: str, agent_name: str | None, as_json: bool) -> None:
    """List deployed verification checks."""

    rows = _repository().get_checks(run_id, agent_name)
    if as_json:
        _json_echo(rows)
        return
    for row in rows:
        click.echo(f"{row.get('created_at')} {row['agent']} exit={row['exit_code']} {row['command_preview']}")


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def trace(run_id: str, agent_name: str | None, as_json: bool) -> None:
    """Show redacted trace mirror rows."""

    rows = _repository().fetch_trace_spans(run_id, agent_name)
    if as_json:
        _json_echo(rows)
        return
    if not rows:
        click.echo("No trace spans found")
        return
    for row in rows:
        label = row.get("agent") or "_system"
        click.echo(f"{row.get('started_at')} {label} {row.get('name')} {row.get('status')} {row.get('duration_ms')}ms")


@main.command()
@click.argument("run_id")
def cancel(run_id: str) -> None:
    """Cancel a deployed run."""

    repo = _repository()
    cancelled = repo.cancel_run(run_id)
    _coordinator().publish_cancel(run_id)
    click.echo(f"Cancelled run {run_id}")
    click.echo(f"Agents cancelled: {cancelled}")


@main.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Resume eligible deployed agents."""

    repo = _repository()
    coordinator = _coordinator()
    resumed = repo.resume_run(run_id)
    for item in resumed:
        if item["status"] == "queued":
            outbox_id = repo.record_queue_outbox(run_id, item["agent"], "agent_ready", {"run_id": run_id, "agent": item["agent"]})
            coordinator.enqueue_agent(run_id, item["agent"])
            repo.mark_outbox_published(outbox_id)
    click.echo(f"Agents requeued: {len(resumed)}")
    for item in resumed:
        click.echo(f"  {item['agent']}: {item['status']}")


@main.command()
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def provenance(run_id: str, agent_name: str | None, as_json: bool) -> None:
    """Show git provenance for a deployed run."""

    rows = _repository().get_git_provenance(run_id, agent_name)
    if as_json:
        _json_echo(rows)
        return
    for row in rows:
        label = row.get("agent") or "_system"
        click.echo(f"{row.get('created_at')} {label} branch={row.get('branch')} head={row.get('head_sha')}")


@main.group()
def pr() -> None:
    """Pull request commands."""


@pr.command("create")
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--all", "all_agents", is_flag=True, help="Create PRs for all provenance rows")
@click.option("--title-prefix", default="spawnd", show_default=True)
def pr_create(run_id: str, agent_name: str | None, all_agents: bool, title_prefix: str) -> None:
    """Create GitHub PRs from recorded branch provenance."""

    if not agent_name and not all_agents:
        raise click.UsageError("Pass --agent or --all")
    rows = _repository().get_git_provenance(run_id, agent_name)
    for row in rows:
        branch = row.get("branch")
        if not branch:
            click.echo(f"Skipping {row.get('agent')}: no branch recorded", err=True)
            continue
        agent_label = row.get("agent") or "run"
        title = f"{title_prefix}: {run_id}/{agent_label}"
        body = json.dumps({"run_id": run_id, "agent": agent_label, "provenance": row}, indent=2, default=str)
        result = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(result.stderr.strip() or result.stdout.strip(), err=True)
            raise click.Abort()
        click.echo(result.stdout.strip())


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
def serve(host: str, port: int) -> None:
    """Serve the deployed HTTP API."""

    import uvicorn

    uvicorn.run("spawnd.server:create_app", factory=True, host=host, port=port)


@main.command()
@click.argument("role_name", required=False)
def roles(role_name: str | None) -> None:
    """List available roles or show role details."""

    if role_name:
        role = get_role(role_name)
        if not role:
            click.echo(f"Role not found: {role_name}", err=True)
            click.echo(f"Available: {', '.join(BUILTIN_ROLES.keys())}")
            raise click.Abort()
        click.echo(f"Role: {role.name}")
        click.echo(f"Description: {role.description}")
        if role.model:
            click.echo(f"Default model: {role.model}")
        if role.check:
            click.echo(f"Default check: {role.check}")
        click.echo(f"\nSystem prompt:\n{role.system_prompt}")
        return
    click.echo("Available roles:")
    for name, role in BUILTIN_ROLES.items():
        click.echo(f"  {name}: {role.description}")


if __name__ == "__main__":
    main()
