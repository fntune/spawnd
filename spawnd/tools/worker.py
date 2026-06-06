"""Worker coordination tools backed by deployed state."""
from __future__ import annotations

import asyncio
import logging

from spawnd.config import load_backend_config
from spawnd.state.repository import DeployedRepository

logger = logging.getLogger("spawnd.tools.worker")


def _repository() -> DeployedRepository:
    config = load_backend_config()
    if not config.database_url:
        raise RuntimeError("SPAWND_DATABASE_URL is required for spawnd coordination tools")
    return DeployedRepository.from_url(config.database_url)


async def _poll_for_response(run_id: str, clarification_id: str, timeout: int) -> dict | None:
    deadline = asyncio.get_event_loop().time() + timeout
    repo = _repository()
    while asyncio.get_event_loop().time() < deadline:
        response = repo.get_response(run_id, clarification_id)
        if response:
            repo.consume_response(int(response["id"]))
            return response
        await asyncio.sleep(2)
    return None


async def mark_complete(run_id: str, agent_name: str, summary: str) -> str:
    """Record a completion signal.

    The deployed worker runs verification and owns the final state transition.
    """

    _repository().append_event(run_id, agent_name, "completion_signal", {"summary": summary})
    logger.info("Agent %s signalled completion", agent_name)
    return "Completion signal recorded. The worker will run verification before finalizing state."


async def request_clarification(
    run_id: str,
    agent_name: str,
    question: str,
    escalate_to: str = "auto",
    timeout: int = 300,
    *,
    parent: str = "",
    tree_path: str = "",
) -> str:
    """Ask for guidance and wait for a response."""

    repo = _repository()
    event_id = repo.append_event(
        run_id,
        agent_name,
        "clarification",
        {"question": question, "escalate_to": escalate_to, "parent_agent": parent, "tree_path": tree_path},
    )
    response = await _poll_for_response(run_id, event_id, timeout)
    if response:
        return f"Manager response: {response['response']}"
    repo.append_event(run_id, agent_name, "clarification_timeout", {"question": question})
    return "ERROR: Clarification timeout. No response was recorded."


async def report_progress(run_id: str, agent_name: str, status: str, milestone: str | None = None) -> str:
    """Record progress in the event ledger."""

    data = {"status": status}
    if milestone:
        data["milestone"] = milestone
    _repository().append_event(run_id, agent_name, "progress", data)
    return "Progress recorded."


async def report_blocker(
    run_id: str,
    agent_name: str,
    issue: str,
    timeout: int = 300,
    *,
    parent: str = "",
    tree_path: str = "",
) -> str:
    """Report a blocker and wait for guidance."""

    repo = _repository()
    event_id = repo.append_event(
        run_id,
        agent_name,
        "blocker",
        {"question": issue, "parent_agent": parent, "tree_path": tree_path},
    )
    response = await _poll_for_response(run_id, event_id, timeout)
    if response:
        return f"Manager guidance: {response['response']}"
    repo.append_event(run_id, agent_name, "blocker_timeout", {"issue": issue})
    return "ERROR: Blocker timeout. No response was recorded."
