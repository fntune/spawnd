"""Manager coordination tools backed by deployed state."""
from __future__ import annotations

import logging
import re

from spawnd.config import load_backend_config
from spawnd.coordination.redis import RedisCoordinator
from spawnd.artifacts.redaction import redact_freeform_text, stable_hash
from spawnd.state.repository import DeployedRepository

logger = logging.getLogger("spawnd.tools.manager")
VALID_WORKER_NAME = re.compile("^[A-Za-z0-9_.-]+$")


def _repository() -> DeployedRepository:
    config = load_backend_config()
    if not config.database_url:
        raise RuntimeError("SPAWND_DATABASE_URL is required for spawnd coordination tools")
    return DeployedRepository.from_url(config.database_url)


def _coordinator() -> RedisCoordinator | None:
    config = load_backend_config()
    if not config.redis_url:
        return None
    return RedisCoordinator.from_url(config.redis_url)


def _worker_name(manager_name: str, name: str) -> str:
    return name if "." in name else f"{manager_name}.{name}"


async def spawn_worker(run_id: str, manager_name: str, name: str, prompt: str, check: str | None = None, model: str = "sonnet") -> str:
    """Record a worker spawn request.

    Dynamic manager spawning is represented as a durable event; deployed run
    scheduling remains owned by submitted plan rows.
    """

    if not VALID_WORKER_NAME.fullmatch(name):
        return f"Invalid worker name: {name}"
    worker_name = _worker_name(manager_name, name)
    _repository().append_event(
        run_id,
        manager_name,
        "spawn_worker_requested",
        {
            "worker": worker_name,
            "prompt_hash": stable_hash(prompt),
            "prompt_preview": redact_freeform_text(prompt[:500]),
            "check_hash": stable_hash(check or ""),
            "check_preview": redact_freeform_text((check or "")[:500]),
            "model": model,
        },
    )
    return f"Spawn request recorded for worker: {worker_name}"


async def respond_to_clarification(run_id: str, manager_name: str, clarification_id: str, response: str) -> str:
    """Respond to a worker clarification or blocker."""

    repo = _repository()
    repo.record_response(run_id, clarification_id, response)
    repo.append_event(run_id, manager_name, "clarification_response", {"clarification_id": clarification_id})
    logger.info("Manager %s responded to clarification %s", manager_name, clarification_id[:8])
    return f"Response sent to clarification {clarification_id[:8]}"


async def cancel_worker(run_id: str, manager_name: str, name: str) -> str:
    """Cancel a worker row through deployed state and publish a cancel hint."""

    worker_name = _worker_name(manager_name, name)
    cancelled = _repository().cancel_agent(run_id, worker_name, "Cancelled by manager")
    coordinator = _coordinator()
    if coordinator is not None:
        coordinator.publish_cancel(run_id)
    if not cancelled:
        return f"Worker not cancellable: {worker_name}"
    return f"Cancelled worker: {worker_name}"


async def get_worker_status(run_id: str, manager_name: str, name: str | None = None) -> str:
    """Return status for one managed worker or all workers with the manager prefix."""

    repo = _repository()
    if name:
        worker_name = _worker_name(manager_name, name)
        agent = repo.get_agent(run_id, worker_name)
        if not agent:
            return f"Worker not found: {name}"
        text = f"Worker: {agent['name']}\nStatus: {agent['status']}"
        if agent.get("error"):
            text += f"\nError: {str(agent['error'])[:200]}"
        return text
    workers = [agent for agent in repo.get_agents(run_id) if str(agent["name"]).startswith(f"{manager_name}.")]
    if not workers:
        return "No workers found."
    lines = ["Workers:"]
    for worker in workers:
        lines.append(f"  {str(worker['name']).split('.')[-1]}: {worker['status']}")
        if worker.get("error"):
            lines.append(f"    Error: {str(worker['error'])[:100]}")
    return "\n".join(lines)


async def get_pending_clarifications(run_id: str, manager_name: str) -> str:
    """List clarification/blocker events awaiting a response."""

    rows = _repository().get_pending_clarifications(run_id, agent_prefix=f"{manager_name}.")
    if not rows:
        return "No pending clarifications."
    lines = ["Pending clarifications:"]
    for row in rows:
        data = row.get("data") or {}
        question = data.get("question") or data.get("issue") or ""
        lines.append(f"  [{str(row['id'])[:8]}] {row['agent']}: {question}")
    return "\n".join(lines)


async def mark_plan_complete(run_id: str, manager_name: str, summary: str) -> str:
    """Record a manager completion signal."""

    _repository().append_event(run_id, manager_name, "manager_completion_signal", {"summary": summary})
    return "Plan completion signal recorded."
