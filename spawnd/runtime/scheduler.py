"""Scheduler for spawnd.dev orchestration."""
import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
import yaml
from spawnd.storage.db import all_agents_done, get_agent, get_agents, get_pending_agents, get_plan, get_recent_events, get_total_cost, init_db, insert_agent, insert_event, insert_plan, open_db, reset_failed_agents, reset_paused_agents, run_exists, update_agent_worktree, update_plan_status
from spawnd.runtime.executor import AgentConfig, spawn_manager, spawn_worker
from spawnd.runtime.agent_config import hydrate_agent_runtime_config, resolve_agent_plan_config
from spawnd.runtime.agent_state import transition_agent_status
from spawnd.runtime.policies.budget import apply_run_budget_policy
from spawnd.runtime.policies.circuit_breaker import apply_circuit_breaker_policy
from spawnd.runtime.policies.failure import apply_failure_policy
from spawnd.runtime.policies.stuck import evaluate_stuck_run
from spawnd.runtime.task_registry import CancellationRegistry, get_default_registry
from spawnd.gitops.worktrees import create_worktree, get_repo_root, run_worktree_setup, setup_worktree_with_deps
from spawnd.models.specs import PlanSpec
from spawnd.io.plan_builder import load_shared_context
from spawnd.io.parser import generate_run_id
from spawnd.storage.repository import transaction
logger = logging.getLogger('spawnd.scheduler')
STUCK_THRESHOLD_ITERATIONS = 30
POLL_INTERVAL_SECONDS = 1.0

async def _failed_spawn_result(error: str) -> dict:
    """Return a task-shaped failed spawn result without launching an agent."""
    return {'success': False, 'status': 'failed', 'error': error}

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

    def __init__(self, plan: PlanSpec, run_id: str | None=None, use_mock: bool=False, resume: bool=False, cancellation_registry: CancellationRegistry | None=None):
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
        self._db: sqlite3.Connection | None = None
        self.failure_count = 0
        self.idle_iterations = 0
        self.last_event_marker: str | None = None

    @property
    def db(self) -> sqlite3.Connection:
        assert self._db is not None
        return self._db

    @db.setter
    def db(self, conn: sqlite3.Connection | None) -> None:
        self._db = conn

    def _cancel_all_tasks(self, status: str, message: str='', include_pending: bool=False) -> None:
        """Cancel all running tasks and update their status.

        Args:
            status: Status to set for cancelled agents
            message: Optional error message
            include_pending: Also mark pending agents with the status
        """
        for name, task in self.tasks.items():
            if not task.done():
                _ = task.cancel()
                _ = self.cancellation_registry.unregister(self.run_id, name)
                _ = transition_agent_status(self.db, self.run_id, name, status, message or None, current_status='running', force=True)
        if include_pending:
            for a in get_agents(self.db, self.run_id):
                if a['status'] == 'pending':
                    _ = transition_agent_status(self.db, self.run_id, a['name'], status, message or None, current_status='pending', force=True)

    def _init_db(self) -> None:
        """Initialize database and insert plan/agents."""
        if self.resume and run_exists(self.run_id):
            self.db = open_db(self.run_id)
            _ = update_plan_status(self.db, self.run_id, 'running')
            reset_names = reset_failed_agents(self.db, self.run_id)
            if reset_names:
                _ = logger.info(f'Reset agents for retry: {reset_names}')
            resumed_names = reset_paused_agents(self.db, self.run_id)
            if resumed_names:
                _ = logger.info(f'Reset paused agents for resume: {resumed_names}')
            agents = get_agents(self.db, self.run_id)
            completed = [a['name'] for a in agents if a['status'] == 'completed']
            pending = [a['name'] for a in agents if a['status'] == 'pending']
            _ = logger.info(f'Resuming run {self.run_id}: {len(completed)} completed, {len(pending)} pending')
            return
        self.db = init_db(self.run_id)
        with transaction(self.db):
            _ = insert_plan(self.db, self.run_id, self.plan.name, yaml.dump(self.plan.model_dump()), self.plan.cost_budget.total_usd if self.plan.cost_budget else 25.0, autocommit=False)
            defaults = self.plan.defaults
            for agent in self.plan.agents:
                resolved = resolve_agent_plan_config(agent, defaults)
                _ = insert_agent(self.db, self.run_id, agent.name, resolved.prompt, agent_type=agent.type, check_command=resolved.check_command, model=resolved.model, max_iterations=resolved.max_iterations, max_cost_usd=resolved.max_cost_usd, depends_on=agent.depends_on, plan_name=self.plan.name, on_failure=resolved.on_failure, retry_count=resolved.retry_count, env=agent.env or None, max_subagents=resolved.manager_cap, runtime=resolved.runtime, cost_source=resolved.cost_source, autocommit=False)
        _ = logger.info(f'Initialized run {self.run_id} with {len(self.plan.agents)} agents')

    async def _spawn_agent(self, agent_row: sqlite3.Row) -> asyncio.Task:
        """Create worktree and spawn agent.

        Args:
            agent_row: Agent database row

        Returns:
            asyncio.Task handle
        """
        name = agent_row['name']
        agent_type = agent_row['type'] or 'worker'
        source = self.plan.orchestration.worktree_source if self.plan.orchestration else None
        worktree_path = create_worktree(self.run_id, name, base_ref=source.base_ref if source else None, fetch=source.fetch if source else False)
        depends_on = json.loads(agent_row['depends_on'] or '[]')
        if depends_on:
            dep_context = self.plan.orchestration.dependency_context if self.plan.orchestration else None
            _ = setup_worktree_with_deps(self.run_id, name, depends_on, worktree_path, mode=dep_context.mode if dep_context else 'full', include_paths=dep_context.include_paths if dep_context else None, exclude_paths=dep_context.exclude_paths if dep_context else None)
        _ = update_agent_worktree(self.db, self.run_id, name, str(worktree_path), f'spawnd/{self.run_id}/{name}')
        shared_context = ''
        if self.plan.shared_context:
            shared_context = load_shared_context(self.plan.shared_context)
        error_context = self._build_error_context(agent_row)
        prompt = agent_row['prompt']
        if error_context:
            prompt = f'{prompt}\n\n{error_context}'
        hydrated = hydrate_agent_runtime_config(agent_row, prompt)
        setup = self.plan.orchestration.worktree_setup if self.plan.orchestration else None
        if setup:
            source_path = get_repo_root()
            setup_env = dict(hydrated.env or {})
            _ = setup_env.update(setup.env)
            _ = insert_event(self.db, self.run_id, name, 'worktree_setup_started', {'command': setup.command})
            try:
                result = run_worktree_setup(worktree_path, source_path, setup.command, env=setup_env, timeout_seconds=setup.timeout_seconds)
            except Exception as exc:
                error = str(exc)
                _ = transition_agent_status(self.db, self.run_id, name, 'failed', error, current_status=agent_row['status'], force=True)
                _ = insert_event(self.db, self.run_id, name, 'worktree_setup_failed', {'error': error})
                return asyncio.create_task(_failed_spawn_result(error), name=f'setup-failed-{name}')
            _ = insert_event(self.db, self.run_id, name, 'worktree_setup_completed', {'stdout': result.stdout[-2000:], 'stderr': result.stderr[-2000:]})
        config = AgentConfig(name=name, run_id=self.run_id, prompt=hydrated.prompt, worktree=worktree_path, check_command=hydrated.check_command, model=hydrated.model, max_iterations=hydrated.max_iterations, max_cost_usd=hydrated.max_cost_usd, parent=agent_row['parent'], env=hydrated.env, shared_context=shared_context, runtime=hydrated.runtime)
        if agent_type == 'manager':
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
        depends_on = json.loads(agent_row['depends_on'] or '[]')
        if not depends_on:
            return []
        failed = []
        for dep_name in depends_on:
            dep = get_agent(self.db, self.run_id, dep_name)
            if dep and dep['status'] in ('failed', 'timeout', 'cancelled', 'cost_exceeded'):
                _ = failed.append(dep_name)
        return failed

    async def _handle_cost_exceeded(self) -> bool:
        """Apply run-level budget policy. Returns True if run should stop."""
        total_cost = get_total_cost(self.db, self.run_id)
        budget = self.plan.cost_budget.total_usd if self.plan.cost_budget else 25.0
        action = self.plan.cost_budget.on_exceed if self.plan.cost_budget else 'pause'
        return apply_run_budget_policy(self.db, self.run_id, total_cost, budget, action, self._cancel_all_tasks)

    async def _handle_agent_failure(self, name: str, error: str) -> bool:
        """Apply per-agent on_failure policy. Returns True if run should stop."""
        self.failure_count += 1
        return apply_failure_policy(self.db, self.run_id, name, error, self._cancel_all_tasks)

    def _build_error_context(self, agent_row: sqlite3.Row) -> str:
        """Build error context for retried agents."""
        last_error = agent_row['last_error']
        retry_attempt = agent_row['retry_attempt'] or 0
        if not last_error or retry_attempt == 0:
            return ''
        return f'\n## Previous Attempt Failed\n\nThis is retry attempt {retry_attempt}. The previous attempt failed with:\n\n```\n{last_error[:500]}\n```\n\nPlease address this error and continue with the task.\n'

    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker should trip.

        Returns True if run should stop.
        """
        if not self.plan.orchestration or not self.plan.orchestration.circuit_breaker:
            return False
        cb = self.plan.orchestration.circuit_breaker
        return apply_circuit_breaker_policy(self.db, self.run_id, self.failure_count, cb.threshold, cb.action, self._cancel_all_tasks)

    def _check_stuck(self) -> bool:
        """Check for stuck agents (no progress).

        Returns True if run appears stuck.
        """
        stuck_threshold = STUCK_THRESHOLD_ITERATIONS
        if self.plan.orchestration and self.plan.orchestration.stuck_threshold is not None:
            stuck_threshold = self.plan.orchestration.stuck_threshold
        events = get_recent_events(self.db, self.run_id, since_seconds=60)
        is_stuck, marker, idle = evaluate_stuck_run(self.db, self.run_id, events=events, has_live_tasks=bool(self.tasks), last_event_marker=self.last_event_marker, idle_iterations=self.idle_iterations, threshold=stuck_threshold)
        self.last_event_marker = marker
        self.idle_iterations = idle
        return is_stuck

    def _build_result(self) -> SchedulerResult:
        """Build scheduler result from current state."""
        agents = get_agents(self.db, self.run_id)
        completed = [a['name'] for a in agents if a['status'] == 'completed']
        failed = [a['name'] for a in agents if a['status'] in ('failed', 'timeout', 'cancelled', 'cost_exceeded')]
        total_cost = get_total_cost(self.db, self.run_id)
        success = len(failed) == 0 and len(completed) == len(agents)
        current_plan = get_plan(self.db, self.run_id)
        current_status = current_plan['status'] if current_plan else 'running'
        error = None
        if current_status in ('paused', 'cancelled'):
            success = False
            error = f'Run {current_status}'
        else:
            plan_status = 'completed' if success else 'failed'
            _ = update_plan_status(self.db, self.run_id, plan_status)
        return SchedulerResult(run_id=self.run_id, success=success, completed=completed, failed=failed, total_cost=total_cost, error=error)

    async def run(self) -> SchedulerResult:
        """Execute the plan.

        Returns:
            SchedulerResult with execution details
        """
        _ = self._init_db()
        try:
            while not all_agents_done(self.db, self.run_id):
                plan = get_plan(self.db, self.run_id)
                if plan and plan['status'] == 'cancelled':
                    _ = logger.info('Run cancelled externally')
                    _ = self._cancel_all_tasks('cancelled', 'Run cancelled externally', include_pending=True)
                    break
                ready = get_pending_agents(self.db, self.run_id)
                for row in ready:
                    failed_deps = self._check_failed_deps(row)
                    if failed_deps:
                        _ = transition_agent_status(self.db, self.run_id, row['name'], 'failed', f'Dependency failed: {failed_deps}', current_status=row['status'], force=True)
                        _ = insert_event(self.db, self.run_id, row['name'], 'cascade_skip', {'failed_deps': failed_deps})
                        continue
                    if row['name'] not in self.tasks:
                        task = await self._spawn_agent(row)
                        self.tasks[row['name']] = task
                        _ = self.cancellation_registry.register(self.run_id, row['name'], task)
                        _ = logger.info(f"Spawned agent {row['name']}")
                should_stop = False
                for name, task in list(self.tasks.items()):
                    if task.done():
                        try:
                            result = task.result()
                            _ = logger.info(f'Agent {name} finished: {result}')
                            agent = get_agent(self.db, self.run_id, name)
                            if agent and agent['status'] == 'failed':
                                error = agent['error'] or 'Unknown error'
                                should_stop = await self._handle_agent_failure(name, error)
                        except asyncio.CancelledError:
                            _ = logger.info(f'Agent {name} was cancelled')
                        except Exception as e:
                            _ = logger.error(f'Agent {name} raised exception: {e}')
                            _ = transition_agent_status(self.db, self.run_id, name, 'failed', str(e), current_status='running', force=True)
                            should_stop = await self._handle_agent_failure(name, str(e))
                        del self.tasks[name]
                        _ = self.cancellation_registry.unregister(self.run_id, name)
                        if should_stop:
                            break
                if should_stop:
                    break
                if self._check_circuit_breaker():
                    break
                if self.plan.cost_budget:
                    total_cost = get_total_cost(self.db, self.run_id)
                    if total_cost > self.plan.cost_budget.total_usd:
                        if await self._handle_cost_exceeded():
                            break
                _ = self._check_stuck()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            if self.tasks:
                _ = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            return self._build_result()
        finally:
            _ = self.cancellation_registry.clear_run(self.run_id)
            if self._db is not None:
                _ = self._db.close()
                self._db = None

async def run_plan(plan: PlanSpec, run_id: str | None=None, use_mock: bool=False, resume: bool=False) -> SchedulerResult:
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
