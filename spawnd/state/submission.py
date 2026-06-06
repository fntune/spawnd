"""Helpers for deployed run submission and worker claims."""
from __future__ import annotations

from uuid import uuid4

from spawnd.coordination.redis import AgentJob, CoordinationPlane
from spawnd.state.repository import ClaimedAgent, DeployedRepository
from spawnd.io.parser import generate_run_id
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
