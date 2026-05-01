"""Scheduler for spawnd.dev orchestration."""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass

import yaml

from spawnd.storage.db import (
    all_agents_done,
    get_agent,
    get_agents,
    get_pending_agents,
    get_plan,
    get_recent_events,
    get_total_cost,
    init_db,
    insert_agent,
    insert_event,
    insert_plan,
    open_db,
    reset_failed_agents,
    reset_paused_agents,
    run_exists,
    update_agent_worktree,
    update_plan_status,
)
from spawnd.runtime.executor import AgentConfig, spawn_manager, spawn_worker
from spawnd.runtime.agent_config import hydrate_agent_runtime_config, resolve_agent_plan_config
from spawnd.runtime.agent_state import transition_agent_status
from spawnd.runtime.policies.budget import apply_run_budget_policy
from spawnd.runtime.policies.circuit_breaker import apply_circuit_breaker_policy
from spawnd.runtime.policies.failure import apply_failure_policy
from spawnd.runtime.policies.stuck import evaluate_stuck_run
from spawnd.runtime.task_registry import CancellationRegistry, get_default_registry
from spawnd.gitops.worktrees import create_worktree, setup_worktree_with_deps
from spawnd.models.specs import PlanSpec
from spawnd.io.plan_builder import load_shared_context
from spawnd.io.parser import generate_run_id
from spawnd.storage.repository import transaction

logger = logging.getLogger("spawnd.scheduler")

# Scheduler constants
STUCK_THRESHOLD_ITERATIONS = 30
POLL_INTERVAL_SECONDS = 1.0


@dataclass
class SchedulerResult:
    """Result of scheduler execution."""

    run_id: str
    success: bool
    completed: list[str]
    failed: list[str]
    total_cost: float
    error: str | None = None


class Scheduler:
    """Orchestrates agent execution."""

    def __init__(
        self,
        plan: PlanSpec,
        run_id: str | None = None,
        use_mock: bool = False,
        resume: bool = False,
        cancellation_registry: CancellationRegistry | None = None,
    ):
        """Initialize scheduler.

        Args:
            plan: Plan specification
            run_id: Optional run ID (generated if not provided)
            use_mock: Use mock workers (for testing)
            resume: Resume existing run (skip completed agents, reset failed)
        """
        self.plan = plan
        self.run_id = run_id or generate_run_id(plan.name)
        self.use_mock = use_mock
        self.resume = resume
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancellation_registry = cancellation_registry or get_default_registry()
        self.db: sqlite3.Connection = None  # type: ignore[assignment]  # Set in run()
        self.failure_count = 0
        self.idle_iterations = 0
        self.last_event_marker: str | None = None

    def _cancel_all_tasks(self, status: str, message: str = "", include_pending: bool = False) -> None:
        """Cancel all running tasks and update their status.

        Args:
            status: Status to set for cancelled agents
            message: Optional error message
            include_pending: Also mark pending agents with the status
        """
        for name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                self.cancellation_registry.unregister(self.run_id, name)
                transition_agent_status(
                    self.db,
                    self.run_id,
                    name,
                    status,
                    message or None,
                    current_status="running",
                    force=True,
                )

        if include_pending:
            for a in get_agents(self.db, self.run_id):
                if a["status"] == "pending":
                    transition_agent_status(
                        self.db,
                        self.run_id,
                        a["name"],
                        status,
                        message or None,
                        current_status="pending",
                        force=True,
                    )

    def _init_db(self) -> None:
        """Initialize database and insert plan/agents."""
        if self.resume and run_exists(self.run_id):
            # Resume existing run
            self.db = open_db(self.run_id)
            update_plan_status(self.db, self.run_id, "running")

            # Reset failed/timeout agents for retry
            reset_names = reset_failed_agents(self.db, self.run_id)
            if reset_names:
                logger.info(f"Reset agents for retry: {reset_names}")

            resumed_names = reset_paused_agents(self.db, self.run_id)
            if resumed_names:
                logger.info(f"Reset paused agents for resume: {resumed_names}")

            agents = get_agents(self.db, self.run_id)
            completed = [a["name"] for a in agents if a["status"] == "completed"]
            pending = [a["name"] for a in agents if a["status"] == "pending"]
            logger.info(f"Resuming run {self.run_id}: {len(completed)} completed, {len(pending)} pending")
            return

        # New run - initialize fresh
        self.db = init_db(self.run_id)

        with transaction(self.db):
            insert_plan(
                self.db,
                self.run_id,
                self.plan.name,
                yaml.dump(self.plan.model_dump()),
                self.plan.cost_budget.total_usd if self.plan.cost_budget else 25.0,
                autocommit=False,
            )

            defaults = self.plan.defaults
            for agent in self.plan.agents:
                resolved = resolve_agent_plan_config(agent, defaults)
                insert_agent(
                    self.db,
                    self.run_id,
                    agent.name,
                    resolved.prompt,
                    agent_type=agent.type,
                    check_command=resolved.check_command,
                    model=resolved.model or "sonnet",
                    max_iterations=resolved.max_iterations,
                    max_cost_usd=resolved.max_cost_usd,
                    depends_on=agent.depends_on,
                    plan_name=self.plan.name,
                    on_failure=resolved.on_failure,
                    retry_count=resolved.retry_count,
                    env=agent.env or None,
                    max_subagents=resolved.manager_cap,
                    runtime=resolved.runtime,
                    cost_source=resolved.cost_source,
                    autocommit=False,
                )

        logger.info(f"Initialized run {self.run_id} with {len(self.plan.agents)} agents")

    async def _spawn_agent(self, agent_row: sqlite3.Row) -> asyncio.Task:
        """Create worktree and spawn agent.

        Args:
            agent_row: Agent database row

        Returns:
            asyncio.Task handle
        """
        name = agent_row["name"]
        agent_type = agent_row["type"] or "worker"

        # Create worktree
        worktree_path = create_worktree(self.run_id, name)

        # Merge dependencies
        depends_on = json.loads(agent_row["depends_on"] or "[]")
        if depends_on:
            dep_context = self.plan.orchestration.dependency_context if self.plan.orchestration else None
            setup_worktree_with_deps(
                self.run_id,
                name,
                depends_on,
                worktree_path,
                mode=dep_context.mode if dep_context else "full",
                include_paths=dep_context.include_paths if dep_context else None,
                exclude_paths=dep_context.exclude_paths if dep_context else None,
            )

        # Update DB with worktree info
        update_agent_worktree(
            self.db,
            self.run_id,
            name,
            str(worktree_path),
            f"spawnd/{self.run_id}/{name}",
        )

        # Load shared context
        shared_context = ""
        if self.plan.shared_context:
            shared_context = load_shared_context(self.plan.shared_context)

        # Add error context for retries
        error_context = self._build_error_context(agent_row)
        prompt = agent_row["prompt"]
        if error_context:
            prompt = f"{prompt}\n\n{error_context}"

        hydrated = hydrate_agent_runtime_config(agent_row, prompt)

        # Build config
        config = AgentConfig(
            name=name,
            run_id=self.run_id,
            prompt=hydrated.prompt,
            worktree=worktree_path,
            check_command=hydrated.check_command,
            model=hydrated.model,
            max_iterations=hydrated.max_iterations,
            max_cost_usd=hydrated.max_cost_usd,
            parent=agent_row["parent"],
            env=hydrated.env,
            shared_context=shared_context,
            runtime=hydrated.runtime,
        )

        # Spawn based on type
        if agent_type == "manager":
            return spawn_manager(config)
        else:
            return spawn_worker(config, use_mock=self.use_mock)

    def _check_failed_deps(self, agent_row: sqlite3.Row) -> list[str]:
        """Check if agent has failed dependencies.

        Args:
            agent_row: Agent database row

        Returns:
            List of failed dependency names
        """
        depends_on = json.loads(agent_row["depends_on"] or "[]")
        if not depends_on:
            return []

        failed = []
        for dep_name in depends_on:
            dep = get_agent(self.db, self.run_id, dep_name)
            if dep and dep["status"] in ("failed", "timeout", "cancelled", "cost_exceeded"):
                failed.append(dep_name)

        return failed

    async def _handle_cost_exceeded(self) -> bool:
        """Apply run-level budget policy. Returns True if run should stop."""
        total_cost = get_total_cost(self.db, self.run_id)
        budget = self.plan.cost_budget.total_usd if self.plan.cost_budget else 25.0
        action = self.plan.cost_budget.on_exceed if self.plan.cost_budget else "pause"
        return apply_run_budget_policy(
            self.db,
            self.run_id,
            total_cost,
            budget,
            action,
            self._cancel_all_tasks,
        )

    async def _handle_agent_failure(self, name: str, error: str) -> bool:
        """Apply per-agent on_failure policy. Returns True if run should stop."""
        self.failure_count += 1
        return apply_failure_policy(self.db, self.run_id, name, error, self._cancel_all_tasks)

    def _build_error_context(self, agent_row: sqlite3.Row) -> str:
        """Build error context for retried agents."""
        last_error = agent_row["last_error"]
        retry_attempt = agent_row["retry_attempt"] or 0

        if not last_error or retry_attempt == 0:
            return ""

        return f"""
## Previous Attempt Failed

This is retry attempt {retry_attempt}. The previous attempt failed with:

```
{last_error[:500]}
```

Please address this error and continue with the task.
"""

    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker should trip.

        Returns True if run should stop.
        """
        if not self.plan.orchestration or not self.plan.orchestration.circuit_breaker:
            return False

        cb = self.plan.orchestration.circuit_breaker
        return apply_circuit_breaker_policy(
            self.db,
            self.run_id,
            self.failure_count,
            cb.threshold,
            cb.action,
            self._cancel_all_tasks,
        )

    def _check_stuck(self) -> bool:
        """Check for stuck agents (no progress).

        Returns True if run appears stuck.
        """
        stuck_threshold = STUCK_THRESHOLD_ITERATIONS
        if self.plan.orchestration and self.plan.orchestration.stuck_threshold is not None:
            stuck_threshold = self.plan.orchestration.stuck_threshold
        events = get_recent_events(self.db, self.run_id, since_seconds=60)
        is_stuck, marker, idle = evaluate_stuck_run(
            self.db,
            self.run_id,
            events=events,
            has_live_tasks=bool(self.tasks),
            last_event_marker=self.last_event_marker,
            idle_iterations=self.idle_iterations,
            threshold=stuck_threshold,
        )
        self.last_event_marker = marker
        self.idle_iterations = idle
        return is_stuck

    def _build_result(self) -> SchedulerResult:
        """Build scheduler result from current state."""
        agents = get_agents(self.db, self.run_id)

        completed = [a["name"] for a in agents if a["status"] == "completed"]
        failed = [a["name"] for a in agents if a["status"] in ("failed", "timeout", "cancelled", "cost_exceeded")]
        total_cost = get_total_cost(self.db, self.run_id)

        success = len(failed) == 0 and len(completed) == len(agents)

        # Update plan status - preserve paused/cancelled states
        current_plan = get_plan(self.db, self.run_id)
        current_status = current_plan["status"] if current_plan else "running"
        error = None
        if current_status in ("paused", "cancelled"):
            success = False
            error = f"Run {current_status}"
        else:
            plan_status = "completed" if success else "failed"
            update_plan_status(self.db, self.run_id, plan_status)

        return SchedulerResult(
            run_id=self.run_id,
            success=success,
            completed=completed,
            failed=failed,
            total_cost=total_cost,
            error=error,
        )

    async def run(self) -> SchedulerResult:
        """Execute the plan.

        Returns:
            SchedulerResult with execution details
        """
        self._init_db()

        try:
            while not all_agents_done(self.db, self.run_id):
                # Check for external cancellation
                plan = get_plan(self.db, self.run_id)
                if plan and plan["status"] == "cancelled":
                    logger.info("Run cancelled externally")
                    self._cancel_all_tasks("cancelled", "Run cancelled externally", include_pending=True)
                    break

                # Find ready agents
                ready = get_pending_agents(self.db, self.run_id)

                # Check for failed dependencies
                for row in ready:
                    failed_deps = self._check_failed_deps(row)
                    if failed_deps:
                        transition_agent_status(
                            self.db,
                            self.run_id,
                            row["name"],
                            "failed",
                            f"Dependency failed: {failed_deps}",
                            current_status=row["status"],
                            force=True,
                        )
                        insert_event(
                            self.db,
                            self.run_id,
                            row["name"],
                            "cascade_skip",
                            {"failed_deps": failed_deps},
                        )
                        continue

                    # Spawn if not already running
                    if row["name"] not in self.tasks:
                        task = await self._spawn_agent(row)
                        self.tasks[row["name"]] = task
                        self.cancellation_registry.register(self.run_id, row["name"], task)
                        logger.info(f"Spawned agent {row['name']}")

                # Clean up completed tasks and handle failures
                should_stop = False
                for name, task in list(self.tasks.items()):
                    if task.done():
                        try:
                            result = task.result()
                            logger.info(f"Agent {name} finished: {result}")

                            # Check if agent failed
                            agent = get_agent(self.db, self.run_id, name)
                            if agent and agent["status"] == "failed":
                                error = agent["error"] or "Unknown error"
                                should_stop = await self._handle_agent_failure(name, error)

                        except asyncio.CancelledError:
                            logger.info(f"Agent {name} was cancelled")
                        except Exception as e:
                            logger.error(f"Agent {name} raised exception: {e}")
                            transition_agent_status(
                                self.db,
                                self.run_id,
                                name,
                                "failed",
                                str(e),
                                current_status="running",
                                force=True,
                            )
                            should_stop = await self._handle_agent_failure(name, str(e))

                        del self.tasks[name]
                        self.cancellation_registry.unregister(self.run_id, name)

                        if should_stop:
                            break

                if should_stop:
                    break

                # Check circuit breaker
                if self._check_circuit_breaker():
                    break

                # Check cost budget
                if self.plan.cost_budget:
                    total_cost = get_total_cost(self.db, self.run_id)
                    if total_cost > self.plan.cost_budget.total_usd:
                        if await self._handle_cost_exceeded():
                            break
                        # warn action returns False, continue execution

                # Check for stuck condition (but don't stop, just log)
                self._check_stuck()

                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            # Wait for any remaining tasks
            if self.tasks:
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)

            return self._build_result()

        finally:
            self.cancellation_registry.clear_run(self.run_id)
            if self.db:
                self.db.close()


async def run_plan(
    plan: PlanSpec,
    run_id: str | None = None,
    use_mock: bool = False,
    resume: bool = False,
) -> SchedulerResult:
    """Run a plan.

    Args:
        plan: Plan specification
        run_id: Optional run ID
        use_mock: Use mock workers
        resume: Resume existing run

    Returns:
        SchedulerResult
    """
    scheduler = Scheduler(plan, run_id, use_mock, resume)
    return await scheduler.run()
