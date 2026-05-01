"""Worker coordination tools for spawnd.dev agents.

Each public function returns a plain string intended for the caller agent.
The Claude content-block wrapping (``{"content": [{"type": "text", "text": ...}]}``)
is applied at the SDK boundary in ``spawnd.tools.factory``; OpenAI
``@function_tool`` wrappers will use the bare string unchanged.
"""

import asyncio
import logging
import subprocess

from spawnd.runtime.agent_state import transition_agent_status
from spawnd.storage.repository import transaction
from spawnd.storage.db import (
    consume_response,
    get_agent,
    get_db,
    get_response,
    insert_event,
)

logger = logging.getLogger("spawnd.tools.worker")


async def _poll_for_response(
    run_id: str,
    agent_name: str,
    clarification_id: str,
    timeout: int,
    response_type: str,
) -> dict | None:
    """Poll for manager response to clarification/blocker."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        with get_db(run_id) as db:
            response = get_response(db, run_id, clarification_id)
            if response:
                with transaction(db):
                    consume_response(db, response["id"], autocommit=False)
                    transition_agent_status(
                        db,
                        run_id,
                        agent_name,
                        "running",
                        current_status="blocked",
                        force=True,
                    )
                logger.info(f"Agent {agent_name} received {response_type}: {response['response']}")
                return dict(response)

        await asyncio.sleep(2)

    return None


async def mark_complete(run_id: str, agent_name: str, summary: str) -> str:
    """Signal task completion. Runs check command automatically."""
    with get_db(run_id) as db:
        agent = get_agent(db, run_id, agent_name)
        if not agent:
            return f"ERROR: Agent {agent_name} not found"

        if agent["status"] in ("cancelled", "failed", "timeout", "cost_exceeded"):
            logger.warning(
                f"Refusing mark_complete for {agent_name}: agent is in terminal state {agent['status']}"
            )
            return f"ERROR: Agent is in terminal state '{agent['status']}'. mark_complete ignored."

        check_cmd = agent["check_command"] or "true"
        worktree = agent["worktree"]
        logger.info(f"Running check command: {check_cmd}")
        result = subprocess.run(
            check_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=worktree,
        )

        if result.returncode == 0:
            transition_agent_status(
                db,
                run_id,
                agent_name,
                "completed",
                current_status=agent["status"],
                force=True,
            )
            insert_event(db, run_id, agent_name, "done", {"summary": summary})
            logger.info(f"Agent {agent_name} completed successfully")
            return "Task completed successfully. Check passed."
        else:
            output = f"{result.stdout}\n{result.stderr}".strip()
            logger.warning(f"Check failed for {agent_name}: {output}")
            return f"Check failed. Fix and retry.\n\nOutput:\n{output}"


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
    """Ask manager for guidance. BLOCKS until response received."""
    with get_db(run_id) as db:
        with transaction(db):
            event_id = insert_event(
                db,
                run_id,
                agent_name,
                "clarification",
                {
                    "question": question,
                    "escalate_to": escalate_to,
                    "parent_agent": parent,
                    "tree_path": tree_path,
                },
                autocommit=False,
            )
            transition_agent_status(
                db,
                run_id,
                agent_name,
                "blocked",
                current_status="running",
                force=True,
            )
        logger.info(f"Agent {agent_name} blocked on clarification: {question}")

    response = await _poll_for_response(run_id, agent_name, event_id, timeout, "response")
    if response:
        return f"Manager response: {response['response']}"

    with get_db(run_id) as db:
        with transaction(db):
            transition_agent_status(
                db,
                run_id,
                agent_name,
                "timeout",
                "Clarification timeout",
                current_status="blocked",
                force=True,
            )
            insert_event(
                db,
                run_id,
                agent_name,
                "error",
                {"error": "Clarification timeout", "question": question},
                autocommit=False,
            )
    logger.error(f"Agent {agent_name} timed out waiting for clarification")
    return "ERROR: Clarification timeout. No response from manager."


async def report_progress(run_id: str, agent_name: str, status: str, milestone: str | None = None) -> str:
    """Report progress update."""
    with get_db(run_id) as db:
        data = {"status": status}
        if milestone:
            data["milestone"] = milestone

        insert_event(db, run_id, agent_name, "progress", data)
        logger.info(f"Agent {agent_name} progress: {status}" + (f" (milestone: {milestone})" if milestone else ""))
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
    """Report blocking issue. BLOCKS until manager responds."""
    with get_db(run_id) as db:
        with transaction(db):
            event_id = insert_event(
                db,
                run_id,
                agent_name,
                "blocker",
                {
                    "question": issue,
                    "escalate_to": "parent",
                    "parent_agent": parent,
                    "tree_path": tree_path,
                },
                autocommit=False,
            )
            transition_agent_status(
                db,
                run_id,
                agent_name,
                "blocked",
                current_status="running",
                force=True,
            )
        logger.info(f"Agent {agent_name} blocked on issue: {issue}")

    response = await _poll_for_response(run_id, agent_name, event_id, timeout, "guidance")
    if response:
        return f"Manager guidance: {response['response']}"

    with get_db(run_id) as db:
        with transaction(db):
            transition_agent_status(
                db,
                run_id,
                agent_name,
                "timeout",
                "Blocker timeout",
                current_status="blocked",
                force=True,
            )
            insert_event(
                db,
                run_id,
                agent_name,
                "error",
                {"error": "Blocker timeout", "issue": issue},
                autocommit=False,
            )
    logger.error(f"Agent {agent_name} timed out waiting for blocker resolution")
    return "ERROR: Blocker timeout. No response from manager."
