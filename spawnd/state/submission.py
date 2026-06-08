"""Helpers for deployed run submission and worker claims."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from spawnd.coordination.redis import AgentJob, CoordinationPlane
from spawnd.state.repository import ClaimedAgent, DeployedRepository
from spawnd.io.parser import generate_run_id
from spawnd.io.templates import render_plan_template, render_template_text
from spawnd.io.validation import validate_plan
from spawnd.models.specs import PlanSpec


def submit_plan(
    plan: PlanSpec,
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    run_id: str | None = None,
    source_repo: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Persist a run in Postgres and enqueue ready agents in Redis."""

    _validate_or_raise(plan)
    actual_run_id = run_id or generate_run_id(plan.name)
    repository.create_run(plan, actual_run_id, source_repo=source_repo, source_ref=source_ref)
    for agent_name in repository.ready_agents(actual_run_id):
        outbox_id = repository.record_queue_outbox(
            actual_run_id,
            agent_name,
            'agent_ready',
            {'run_id': actual_run_id, 'agent': agent_name},
        )
        coordinator.enqueue_agent(actual_run_id, agent_name)
        repository.mark_outbox_published(outbox_id)
    coordinator.publish_event(actual_run_id, {'type': 'run_submitted', 'run_id': actual_run_id})
    return actual_run_id


def enqueue_submission(coordinator: CoordinationPlane, payload: dict[str, Any]) -> None:
    """Publish a run-submission request to the coordination queue."""

    coordinator.enqueue_submission(payload)


def submit_queued_message(
    payload: dict[str, Any],
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
) -> str:
    """Validate and submit one queued run-submission message."""

    if not isinstance(payload, dict):
        raise ValueError('submission payload must be an object')
    kind = str(payload.get('kind') or ('template' if payload.get('template_id') else 'plan'))
    run_id = _optional_str(payload.get('run_id'))
    if kind == 'template':
        template_id = _required_str(payload.get('template_id'), 'template_id')
        parameters = payload.get('parameters') or {}
        if not isinstance(parameters, dict):
            raise ValueError('parameters must be an object')
        return submit_template(
            template_id,
            parameters=parameters,
            repository=repository,
            coordinator=coordinator,
            run_id=run_id,
        )
    if kind == 'plan':
        plan_raw = payload.get('plan')
        if not isinstance(plan_raw, dict):
            raise ValueError('plan must be an object')
        plan = PlanSpec(**plan_raw)
        return submit_plan(
            plan,
            repository=repository,
            coordinator=coordinator,
            run_id=run_id,
            source_repo=_optional_str(payload.get('source_repo')),
            source_ref=_optional_str(payload.get('source_ref')),
        )
    raise ValueError('kind must be one of: plan, template')


def consume_next_submission(
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    consumer_id: str,
    block_ms: int = 1000,
) -> dict[str, Any] | None:
    """Consume one queued submission and create a run from it."""

    job = coordinator.read_submission(consumer_id, block_ms=block_ms)
    if job is None:
        return None
    try:
        run_id = submit_queued_message(job.payload, repository=repository, coordinator=coordinator)
    except ValueError as exc:
        coordinator.ack_submission(job)
        return {'status': 'rejected', 'error': str(exc)}
    coordinator.ack_submission(job)
    return {'status': 'submitted', 'run_id': run_id}


def submit_template(
    template_id: str,
    *,
    parameters: dict,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    run_id: str | None = None,
) -> str:
    """Render and submit a stored run template."""

    template = repository.get_run_template(template_id)
    if template is None:
        raise ValueError(f'Run template not found: {template_id}')
    plan = render_plan_template(str(template['plan_template']), parameters)
    source_repo = (
        render_template_text(str(template['source_repo_template']), parameters)
        if template.get('source_repo_template')
        else None
    )
    source_ref = (
        render_template_text(str(template['source_ref_template']), parameters)
        if template.get('source_ref_template')
        else None
    )
    return submit_plan(
        plan,
        repository=repository,
        coordinator=coordinator,
        run_id=run_id,
        source_repo=source_repo,
        source_ref=source_ref,
    )


def submit_due_schedules(
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    limit: int = 100,
) -> list[dict]:
    """Submit runs for due durable schedules."""

    submitted = []
    for schedule in repository.due_schedules(limit=limit):
        run_id = submit_template(
            str(schedule['template_id']),
            parameters=dict(schedule.get('parameters') or {}),
            repository=repository,
            coordinator=coordinator,
        )
        repository.mark_schedule_submitted(str(schedule['id']), run_id)
        submitted.append({'schedule_id': schedule['id'], 'run_id': run_id})
    return submitted


def enqueue_newly_ready_agents(
    run_id: str,
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
) -> list[str]:
    """Move dependency-unblocked agents to queued and enqueue them."""

    ready = repository.mark_newly_ready_agents(run_id)
    for agent_name in ready:
        outbox_id = repository.record_queue_outbox(run_id, agent_name, 'agent_ready', {'run_id': run_id, 'agent': agent_name})
        coordinator.enqueue_agent(run_id, agent_name)
        repository.mark_outbox_published(outbox_id)
        coordinator.publish_event(run_id, {'type': 'agent_queued', 'agent': agent_name})
    return ready


def claim_next_agent(
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    worker_id: str,
    lease_seconds: int = 300,
    block_ms: int = 1000,
) -> tuple[AgentJob, ClaimedAgent] | None:
    """Read a Redis job and claim it canonically in Postgres."""

    job = coordinator.read_agent(worker_id, block_ms=block_ms)
    if job is None:
        return None
    claimed = repository.claim_agent(
        job.run_id,
        job.agent,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    coordinator.ack_agent(job)
    if claimed is None:
        return None
    coordinator.set_lease(claimed.run_id, claimed.name, claimed.lease_token, lease_seconds)
    coordinator.heartbeat(worker_id)
    coordinator.publish_event(claimed.run_id, {'type': 'agent_claimed', 'agent': claimed.name, 'worker_id': worker_id})
    return (job, claimed)


def worker_id(prefix: str = 'worker') -> str:
    """Generate a process-unique worker id."""

    return f'{prefix}-{uuid4().hex[:12]}'


def _validate_or_raise(plan: PlanSpec) -> None:
    errors = validate_plan(plan)
    if errors:
        raise ValueError(f"Invalid plan: {'; '.join(errors)}")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_str(value: Any, field: str) -> str:
    if value is None or str(value) == '':
        raise ValueError(f'{field} is required')
    return str(value)
