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
from spawnd.gitops.worktrees import push_branch
from spawnd.io.parser import generate_run_id, parse_plan_file
from spawnd.io.plan_builder import create_inline_plan
from spawnd.io.validation import validate_plan
from spawnd.models.specs import Defaults, PlanSpec
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.roles import BUILTIN_ROLES, get_role
from spawnd.state.repository import DeployedRepository
from spawnd.state.submission import consume_next_submission, enqueue_submission, submit_due_schedules, submit_plan, submit_template, worker_id as make_worker_id
from spawnd.workers.worker import DeployedWorker, drain_queue_outbox, reconcile_ready_agents


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


def _parse_params(params: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in params:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise click.UsageError(f"Invalid --param value: {item}. Use key=value")
        parsed[key] = value
    return parsed


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


@main.command("drain-outbox")
@click.option("--limit", type=int, default=100, show_default=True)
def drain_outbox(limit: int) -> None:
    """Publish pending queue outbox rows to Redis."""

    published = drain_queue_outbox(_repository(), _coordinator(), limit=limit)
    click.echo(f"Published outbox rows: {len(published)}")
    for item in published:
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


@main.command("workers")
@click.option("--json", "as_json", is_flag=True)
def workers(as_json: bool) -> None:
    """Show deployed worker and queue visibility."""

    repo = _repository()
    coordinator = _coordinator()
    payload = {
        "queue_depth": coordinator.queue_depth(),
        "submission_queue_depth": coordinator.submission_queue_depth(),
        "workers": repo.list_worker_nodes(),
    }
    if as_json:
        _json_echo(payload)
        return
    click.echo(f"Queue depth: {payload['queue_depth']}")
    click.echo(f"Submission queue depth: {payload['submission_queue_depth']}")
    for worker in payload["workers"]:
        stale = " stale" if worker.get("stale") else ""
        click.echo(f"  {worker['worker_id']}: {worker.get('status')}{stale} {worker.get('hostname') or ''}")


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
    """Stream deployed events from Redis with Postgres replay."""

    repo = _repository()
    seen: set[str] = set()
    for row in reversed(repo.get_events(run_id, limit=100)):
        event_id = str(row["id"])
        seen.add(event_id)
        click.echo(f"{row.get('created_at')} {row['agent']} {row['event_type']}")
    try:
        for event in _coordinator().subscribe_events(run_id):
            click.echo(json.dumps(event, default=str, sort_keys=True))
    except Exception as exc:
        click.echo(f"Redis live stream unavailable, falling back to Postgres polling: {exc}", err=True)
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


@main.group()
def templates() -> None:
    """Manage reusable run templates."""


@templates.command("put")
@click.argument("template_id")
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option("-f", "--file", "plan_template_file", type=click.Path(exists=True), required=True)
@click.option("--source-repo-template", default=None)
@click.option("--source-ref-template", default=None)
def template_put(
    template_id: str,
    name: str | None,
    description: str | None,
    plan_template_file: str,
    source_repo_template: str | None,
    source_ref_template: str | None,
) -> None:
    """Create or update a reusable plan template."""

    plan_template = Path(plan_template_file).read_text()
    _repository().create_run_template(
        template_id,
        name=name or template_id,
        description=description,
        plan_template=plan_template,
        source_repo_template=source_repo_template,
        source_ref_template=source_ref_template,
    )
    click.echo(f"Template saved: {template_id}")


@templates.command("list")
@click.option("--json", "as_json", is_flag=True)
def template_list(as_json: bool) -> None:
    """List reusable run templates."""

    rows = _repository().list_run_templates()
    if as_json:
        _json_echo(rows)
        return
    for row in rows:
        click.echo(f"{row['id']}: {row['name']}")


@templates.command("run")
@click.argument("template_id")
@click.option("--param", "params", multiple=True, help="Template parameter as key=value")
@click.option("--run-id", default=None)
def template_run(template_id: str, params: tuple[str, ...], run_id: str | None) -> None:
    """Render and submit a reusable run template."""

    actual_run_id = submit_template(
        template_id,
        parameters=_parse_params(params),
        repository=_repository(),
        coordinator=_coordinator(),
        run_id=run_id,
    )
    click.echo(f"Submitted run: {actual_run_id}")


@main.group()
def schedules() -> None:
    """Manage recurring schedule definitions."""


@schedules.command("put")
@click.argument("schedule_id")
@click.option("--template-id", required=True)
@click.option("--name", default=None)
@click.option("--interval-seconds", type=int, required=True)
@click.option("--param", "params", multiple=True, help="Template parameter as key=value")
def schedule_put(schedule_id: str, template_id: str, name: str | None, interval_seconds: int, params: tuple[str, ...]) -> None:
    """Create or update a recurring schedule."""

    _repository().create_schedule(
        schedule_id,
        template_id=template_id,
        name=name or schedule_id,
        interval_seconds=interval_seconds,
        parameters=_parse_params(params),
    )
    click.echo(f"Schedule saved: {schedule_id}")


@schedules.command("run-due")
@click.option("--limit", type=int, default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def schedules_run_due(limit: int, as_json: bool) -> None:
    """Submit runs for due recurring schedules."""

    submitted = submit_due_schedules(repository=_repository(), coordinator=_coordinator(), limit=limit)
    if as_json:
        _json_echo(submitted)
        return
    click.echo(f"Schedules submitted: {len(submitted)}")
    for item in submitted:
        click.echo(f"  {item['schedule_id']} -> {item['run_id']}")


@main.group("submit-queue")
def submit_queue() -> None:
    """Manage queued run submission ingress."""


@submit_queue.command("enqueue-template")
@click.argument("template_id")
@click.option("--param", "params", multiple=True, help="Template parameter as key=value")
@click.option("--run-id", default=None)
def submit_queue_enqueue_template(template_id: str, params: tuple[str, ...], run_id: str | None) -> None:
    """Enqueue a template run request for asynchronous submission."""

    payload = {
        "kind": "template",
        "template_id": template_id,
        "parameters": _parse_params(params),
    }
    if run_id:
        payload["run_id"] = run_id
    enqueue_submission(_coordinator(), payload)
    click.echo(f"Queued template submission: {template_id}")


@submit_queue.command("enqueue-plan")
@click.option("-f", "--file", "plan_file", type=click.Path(exists=True), required=True)
@click.option("--run-id", default=None)
@click.option("--source-repo", default=None)
@click.option("--source-ref", default=None)
def submit_queue_enqueue_plan(plan_file: str, run_id: str | None, source_repo: str | None, source_ref: str | None) -> None:
    """Enqueue a serialized plan request for asynchronous submission."""

    plan = parse_plan_file(Path(plan_file))
    payload: dict[str, Any] = {"kind": "plan", "plan": plan.model_dump(mode="json")}
    if run_id:
        payload["run_id"] = run_id
    if source_repo:
        payload["source_repo"] = source_repo
    if source_ref:
        payload["source_ref"] = source_ref
    enqueue_submission(_coordinator(), payload)
    click.echo(f"Queued plan submission: {plan.name}")


@submit_queue.command("drain")
@click.option("--once", "run_once_flag", is_flag=True, help="Consume at most one submission")
@click.option("--poll", "run_poll_flag", is_flag=True, help="Continuously consume submissions")
@click.option("--consumer-id", default=None)
@click.option("--block-ms", type=int, default=1000, show_default=True)
@click.option("--idle-sleep-seconds", type=float, default=1.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def submit_queue_drain(
    run_once_flag: bool,
    run_poll_flag: bool,
    consumer_id: str | None,
    block_ms: int,
    idle_sleep_seconds: float,
    as_json: bool,
) -> None:
    """Create runs from queued submission messages."""

    if run_once_flag == run_poll_flag:
        raise click.UsageError("Choose exactly one of --once or --poll")
    consumer_id = consumer_id or make_worker_id("submitter")
    repo = _repository()
    coordinator = _coordinator()
    if run_once_flag:
        result = consume_next_submission(repository=repo, coordinator=coordinator, consumer_id=consumer_id, block_ms=block_ms)
        if as_json:
            _json_echo(result or {"status": "empty"})
            return
        click.echo("No queued submissions" if result is None else json.dumps(result, default=str))
        return
    while True:
        result = consume_next_submission(repository=repo, coordinator=coordinator, consumer_id=consumer_id, block_ms=block_ms)
        if result is None:
            time.sleep(idle_sleep_seconds)
            continue
        if as_json:
            _json_echo(result)
        else:
            click.echo(json.dumps(result, default=str))


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
@click.option("--remote", default="origin", show_default=True)
@click.option("--timeout-seconds", type=int, default=60, show_default=True)
@click.option("--no-push", is_flag=True, help="Skip pushing the recorded branch before PR creation")
def pr_create(run_id: str, agent_name: str | None, all_agents: bool, title_prefix: str, remote: str, timeout_seconds: int, no_push: bool) -> None:
    """Create GitHub PRs from recorded branch provenance."""

    if not agent_name and not all_agents:
        raise click.UsageError("Pass --agent or --all")
    rows = _repository().get_git_provenance(run_id, agent_name)
    for row in rows:
        branch = row.get("branch")
        if not branch:
            click.echo(f"Skipping {row.get('agent')}: no branch recorded", err=True)
            continue
        branch_name = str(branch)
        worktree_locator = row.get("worktree_locator")
        if not no_push:
            push_path = Path(str(worktree_locator)) if worktree_locator else None
            if push_path is None or not push_path.exists():
                diff_stats = row.get("diff_stats") if isinstance(row.get("diff_stats"), dict) else {}
                source_repo = diff_stats.get("source_repo")
                push_path = Path(str(source_repo)) if source_repo else None
            if push_path is None or not push_path.exists():
                raise click.UsageError(f"Cannot push {row.get('agent')}: no worktree or source repository recorded")
            push_branch(push_path, branch_name, remote=remote, timeout_seconds=timeout_seconds)
        agent_label = row.get("agent") or "run"
        title = f"{title_prefix}: {run_id}/{agent_label}"
        body = json.dumps({"run_id": run_id, "agent": agent_label, "provenance": row}, indent=2, default=str)
        result = subprocess.run(
            ["gh", "pr", "create", "--head", branch_name, "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            click.echo(result.stderr.strip() or result.stdout.strip(), err=True)
            raise click.Abort()
        click.echo(result.stdout.strip())


@pr.command("merge")
@click.argument("run_id")
@click.option("-a", "--agent", "agent_name", help="Specific agent name")
@click.option("--all", "all_agents", is_flag=True, help="Merge PRs for all provenance rows")
@click.option("--method", type=click.Choice(["merge", "squash", "rebase"]), default="squash", show_default=True)
@click.option("--delete-branch", is_flag=True, help="Delete remote branch after merge")
@click.option("--timeout-seconds", type=int, default=60, show_default=True)
def pr_merge(run_id: str, agent_name: str | None, all_agents: bool, method: str, delete_branch: bool, timeout_seconds: int) -> None:
    """Merge GitHub PRs recorded in run provenance."""

    if not agent_name and not all_agents:
        raise click.UsageError("Pass --agent or --all")
    rows = _repository().get_git_provenance(run_id, agent_name)
    repo = _repository()
    for row in rows:
        pr_target = row.get("pr_url") or row.get("pr_number")
        if not pr_target:
            click.echo(f"Skipping {row.get('agent')}: no PR recorded", err=True)
            continue
        args = ["gh", "pr", "merge", str(pr_target), f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds)
        if result.returncode != 0:
            click.echo(result.stderr.strip() or result.stdout.strip(), err=True)
            raise click.Abort()
        repo.append_event(
            run_id,
            str(row.get("agent") or "_system"),
            "pr_merged",
            {"pr": str(pr_target), "method": method, "delete_branch": delete_branch},
        )
        click.echo(result.stdout.strip() or f"Merged {pr_target}")


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
