"""Failure handling policy for scheduler."""

import logging
import sqlite3
from typing import Callable

from spawnd.storage.db import (
    get_agent,
    increment_retry_attempt,
    insert_event,
    reset_agent_for_retry,
    update_agent_status,
    update_plan_status,
)

logger = logging.getLogger("spawnd.scheduler.failure_policy")


def apply_failure_policy(
    db: sqlite3.Connection,
    run_id: str,
    agent_name: str,
    error: str,
    cancel_all: Callable[..., None],
) -> bool:
    """Apply per-agent on_failure policy.

    Returns True when scheduler should stop.
    """
    agent = get_agent(db, run_id, agent_name)
    if not agent:
        return False

    on_failure = agent["on_failure"] or "continue"
    if on_failure == "stop":
        logger.warning(f"Agent {agent_name} failed with on_failure=stop, cancelling run")
        update_plan_status(db, run_id, "failed")
        cancel_all("cancelled", "Run stopped due to failure", include_pending=True)
        return True

    if on_failure == "retry":
        retry_count = agent["retry_count"] if agent["retry_count"] is not None else 3
        attempt = increment_retry_attempt(db, run_id, agent_name, error)

        if attempt <= retry_count:
            logger.info(f"Retrying agent {agent_name} (retry {attempt}/{retry_count})")
            reset_agent_for_retry(db, run_id, agent_name)
            insert_event(
                db,
                run_id,
                agent_name,
                "progress",
                {"status": f"Retry attempt {attempt}/{retry_count}", "last_error": error[:200]},
            )
        else:
            logger.warning(f"Agent {agent_name} exhausted retries ({retry_count})")
            update_agent_status(db, run_id, agent_name, "failed", f"Exhausted {retry_count} retries: {error}")

    return False
