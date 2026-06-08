"""Postgres-oriented repository for deployed spawnd state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, and_, create_engine, func, insert, select, update

from spawnd.artifacts.redaction import canonical_json_hash, redact_attributes, redact_env, redact_freeform_text, stable_hash
from spawnd.models.specs import AgentSpec, PlanSpec
from spawnd.runtime.agent_config import resolve_agent_plan_config
from spawnd.state import schema


TERMINAL_AGENT_STATUSES = {'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'}
FAILURE_AGENT_STATUSES = {'failed', 'timeout', 'cost_exceeded'}


@dataclass(frozen=True)
class ClaimedAgent:
    """Agent claim returned to a deployed worker."""

    run_id: str
    name: str
    attempt_id: str
    attempt_number: int
    type: str
    runtime: str
    model: str | None
    branch: str | None
    lease_token: str
    worker_id: str


def _durable_plan_spec(plan: PlanSpec) -> dict[str, Any]:
    """Return the durable run spec without raw agent env values."""

    spec = plan.model_dump(mode='json')
    for agent in spec.get('agents', []):
        if not isinstance(agent, dict):
            continue
        env = agent.get('env') or {}
        agent['env_metadata'] = redact_env(env if isinstance(env, dict) else {})
        agent['env'] = {}
    return spec


class DeployedRepository:
    """Repository boundary for deployed Postgres state.

    The implementation is SQLAlchemy Core, with Postgres as the source of truth.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> 'DeployedRepository':
        return cls(create_engine(database_url, future=True))

    def create_schema(self) -> None:
        """Create all deployed tables. Tests use this; production should use Alembic."""

        schema.metadata.create_all(self.engine)

    def create_run(self, plan: PlanSpec, run_id: str, *, source_repo: str | None = None, source_ref: str | None = None) -> None:
        """Persist a run and its agents."""

        spec = _durable_plan_spec(plan)
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.runs).values(
                    run_id=run_id,
                    name=plan.name,
                    spec=spec,
                    spec_hash=canonical_json_hash(spec),
                    submitted_via='api',
                    status='queued',
                    max_cost_usd=plan.cost_budget.total_usd if plan.cost_budget else 25.0,
                    source_repo=source_repo,
                    source_ref=source_ref,
                )
            )
            for agent in plan.agents:
                resolved = resolve_agent_plan_config(agent, plan.defaults)
                status = 'queued' if not agent.depends_on else 'pending'
                conn.execute(
                    insert(schema.agents).values(
                        run_id=run_id,
                        name=agent.name,
                        plan_name=plan.name,
                        status=status,
                        type=agent.type,
                        runtime=resolved.runtime,
                        model=resolved.model,
                        write_allowed=resolved.write_allowed,
                        prompt_hash=stable_hash(resolved.prompt),
                        prompt_preview=redact_freeform_text(resolved.prompt[:500]),
                        check_command_hash=stable_hash(resolved.check_command or ''),
                        check_command_preview=redact_freeform_text((resolved.check_command or 'true')[:500]),
                        branch=f'spawnd/{run_id}/{agent.name}',
                        depends_on=list(agent.depends_on),
                        on_failure=resolved.on_failure,
                        retry_count=resolved.retry_count,
                        max_cost_usd=resolved.max_cost_usd,
                        cost_source=resolved.cost_source,
                        env_metadata={**redact_env(agent.env), 'env_refs': sorted(agent.env_refs.keys())},
                        max_subagents=resolved.manager_cap,
                    )
                )
            _ = self.append_event_in_transaction(conn, run_id, '_system', 'run_created', {'agent_count': len(plan.agents)})

    def spawn_worker_agent(
        self,
        run_id: str,
        manager_name: str,
        worker_name: str,
        *,
        prompt: str,
        check: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued dynamic worker owned by a manager agent."""

        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            run = conn.execute(
                select(schema.runs).where(schema.runs.c.run_id == run_id).with_for_update()
            ).mappings().first()
            if run is None:
                return {'created': False, 'reason': 'run_not_found'}
            existing = conn.execute(
                select(schema.agents.c.name).where(
                    and_(schema.agents.c.run_id == run_id, schema.agents.c.name == worker_name)
                )
            ).scalar_one_or_none()
            if existing:
                return {'created': False, 'reason': 'agent_exists'}
            manager = conn.execute(
                select(schema.agents).where(
                    and_(schema.agents.c.run_id == run_id, schema.agents.c.name == manager_name)
                )
            ).mappings().first()
            if manager is None:
                return {'created': False, 'reason': 'manager_not_found'}
            manager_cap = manager.get('max_subagents')
            if manager_cap is not None:
                spawned_count = int(
                    conn.execute(
                        select(func.count()).where(
                            and_(
                                schema.agents.c.run_id == run_id,
                                schema.agents.c.name.like(f'{manager_name}.%'),
                            )
                        )
                    ).scalar_one()
                )
                if spawned_count >= int(manager_cap):
                    return {'created': False, 'reason': 'manager_cap_exceeded'}
            spec = dict(run['spec'] or {})
            plan = PlanSpec(**spec)
            dynamic_agent = AgentSpec(
                name=worker_name,
                prompt=prompt,
                check=check,
                model=model,
                runtime=str(manager['runtime'] or plan.defaults.runtime),
            )
            resolved = resolve_agent_plan_config(dynamic_agent, plan.defaults)
            spec_agents = list(spec.get('agents') or [])
            spec_agents.append(dynamic_agent.model_dump(mode='json'))
            spec['agents'] = spec_agents
            conn.execute(
                insert(schema.agents).values(
                    run_id=run_id,
                    name=worker_name,
                    plan_name=run['name'],
                    status='queued',
                    type='worker',
                    runtime=resolved.runtime,
                    model=resolved.model,
                    write_allowed=resolved.write_allowed,
                    prompt_hash=stable_hash(resolved.prompt),
                    prompt_preview=redact_freeform_text(resolved.prompt[:500]),
                    check_command_hash=stable_hash(resolved.check_command or ''),
                    check_command_preview=redact_freeform_text((resolved.check_command or 'true')[:500]),
                    branch=f'spawnd/{run_id}/{worker_name}',
                    depends_on=[],
                    on_failure=resolved.on_failure,
                    retry_count=resolved.retry_count,
                    max_cost_usd=resolved.max_cost_usd,
                    cost_source=resolved.cost_source,
                    env_metadata=redact_env({}),
                    max_subagents=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == run_id)
                .values(spec=spec, spec_hash=canonical_json_hash(spec), updated_at=now)
            )
            _ = self.append_event_in_transaction(
                conn,
                run_id,
                manager_name,
                'spawn_worker_created',
                {'worker': worker_name, 'prompt_hash': stable_hash(prompt), 'check_hash': stable_hash(check or '')},
            )
            return {'created': True, 'agent': worker_name}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(schema.runs).where(schema.runs.c.run_id == run_id)).mappings().first()
            return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.runs)
                .order_by(schema.runs.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
            return [dict(row) for row in rows]

    def create_run_template(
        self,
        template_id: str,
        *,
        name: str,
        plan_template: str,
        description: str | None = None,
        source_repo_template: str | None = None,
        source_ref_template: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            existing = conn.execute(select(schema.run_templates.c.id).where(schema.run_templates.c.id == template_id)).scalar_one_or_none()
            values = {
                'name': name,
                'description': description,
                'plan_template': plan_template,
                'source_repo_template': source_repo_template,
                'source_ref_template': source_ref_template,
                'updated_at': now,
            }
            if existing:
                conn.execute(update(schema.run_templates).where(schema.run_templates.c.id == template_id).values(**values))
            else:
                conn.execute(insert(schema.run_templates).values(id=template_id, created_at=now, **values))

    def get_run_template(self, template_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(schema.run_templates).where(schema.run_templates.c.id == template_id)).mappings().first()
            return dict(row) if row else None

    def list_run_templates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(schema.run_templates).order_by(schema.run_templates.c.created_at.desc()).limit(limit)).mappings().all()
            return [dict(row) for row in rows]

    def create_schedule(
        self,
        schedule_id: str,
        *,
        template_id: str,
        name: str,
        interval_seconds: int,
        parameters: dict[str, Any] | None = None,
        next_run_at: datetime | None = None,
        status: str = 'active',
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError('interval_seconds must be positive')
        now = datetime.now(timezone.utc)
        next_at = next_run_at or now
        with self.engine.begin() as conn:
            existing = conn.execute(select(schema.schedules.c.id).where(schema.schedules.c.id == schedule_id)).scalar_one_or_none()
            values = {
                'template_id': template_id,
                'name': name,
                'status': status,
                'interval_seconds': interval_seconds,
                'parameters': redact_attributes(parameters or {}),
                'next_run_at': next_at,
                'updated_at': now,
            }
            if existing:
                conn.execute(update(schema.schedules).where(schema.schedules.c.id == schedule_id).values(**values))
            else:
                conn.execute(insert(schema.schedules).values(id=schedule_id, created_at=now, **values))

    def due_schedules(self, *, now: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.schedules)
                .where(and_(schema.schedules.c.status == 'active', schema.schedules.c.next_run_at <= now))
                .order_by(schema.schedules.c.next_run_at)
                .limit(limit)
            ).mappings().all()
            return [dict(row) for row in rows]

    def mark_schedule_submitted(self, schedule_id: str, run_id: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(select(schema.schedules).where(schema.schedules.c.id == schedule_id)).mappings().first()
            if row is None:
                return
            next_run_at = now + timedelta(seconds=int(row['interval_seconds']))
            conn.execute(
                update(schema.schedules)
                .where(schema.schedules.c.id == schedule_id)
                .values(last_run_at=now, last_run_id=run_id, next_run_at=next_run_at, updated_at=now)
            )

    def get_agents(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id).order_by(schema.agents.c.created_at)).mappings().all()
            return [dict(row) for row in rows]

    def get_agent(self, run_id: str, agent_name: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(schema.agents).where(
                    and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name)
                )
            ).mappings().first()
            return dict(row) if row else None

    def get_events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.events)
                .where(schema.events.c.run_id == run_id)
                .order_by(schema.events.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
            return [dict(row) for row in rows]

    def get_pending_clarifications(self, run_id: str, *, agent_prefix: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.events)
                .where(
                    and_(
                        schema.events.c.run_id == run_id,
                        schema.events.c.event_type.in_(["clarification", "blocker"]),
                    )
                )
                .order_by(schema.events.c.created_at)
            ).mappings().all()
            results: list[dict[str, Any]] = []
            for row in rows:
                if agent_prefix and not str(row["agent"]).startswith(agent_prefix):
                    continue
                response = conn.execute(
                    select(schema.responses.c.id).where(
                        and_(
                            schema.responses.c.run_id == run_id,
                            schema.responses.c.clarification_id == row["id"],
                        )
                    )
                ).first()
                if response is None:
                    results.append(dict(row))
            return results

    def record_response(self, run_id: str, clarification_id: str, response: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.responses).values(
                    run_id=run_id,
                    clarification_id=clarification_id,
                    response=redact_freeform_text(response),
                    consumed=False,
                )
            )

    def get_response(self, run_id: str, clarification_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(schema.responses)
                .where(
                    and_(
                        schema.responses.c.run_id == run_id,
                        schema.responses.c.clarification_id == clarification_id,
                        schema.responses.c.consumed == False,  # noqa: E712
                    )
                )
                .order_by(schema.responses.c.created_at)
                .limit(1)
            ).mappings().first()
            return dict(row) if row else None

    def consume_response(self, response_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(schema.responses).where(schema.responses.c.id == response_id).values(consumed=True))

    def cancel_agent(self, run_id: str, agent_name: str, error: str = "Agent cancelled") -> bool:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(
                select(schema.agents.c.status).where(
                    and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name)
                )
            ).mappings().first()
            if row is None or row["status"] in TERMINAL_AGENT_STATUSES:
                return False
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name))
                .values(
                    status="cancelled",
                    error=error,
                    lease_token=None,
                    leased_until=None,
                    worker_id=None,
                    heartbeat_at=None,
                    updated_at=now,
                )
            )
            conn.execute(
                update(schema.agent_attempts)
                .where(
                    and_(
                        schema.agent_attempts.c.run_id == run_id,
                        schema.agent_attempts.c.agent == agent_name,
                        schema.agent_attempts.c.status == 'running',
                    )
                )
                .values(status='cancelled', finished_at=now, updated_at=now)
            )
            self.append_event_in_transaction(conn, run_id, agent_name, "agent_cancelled", {"error": error})
        self.refresh_run_status(run_id)
        return True

    def ready_agents(self, run_id: str) -> list[str]:
        """Return queued agents ready for Redis enqueue."""

        with self.engine.connect() as conn:
            return self._claimable_ready_agents_in_connection(conn, run_id)

    def _claimable_ready_agents_in_connection(self, conn: Any, run_id: str) -> list[str]:
        rows = conn.execute(
            select(schema.agents.c.name)
            .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.status == 'queued'))
            .order_by(schema.agents.c.created_at)
        ).all()
        names = [row[0] for row in rows]
        limit = self._concurrency_limit_in_connection(conn, run_id)
        if limit is None:
            return names
        running = int(
            conn.execute(
                select(func.count()).where(and_(schema.agents.c.run_id == run_id, schema.agents.c.status == 'running'))
            ).scalar_one()
        )
        available = max(0, limit - running)
        return names[:available]

    def _concurrency_limit_in_connection(self, conn: Any, run_id: str) -> int | None:
        spec = conn.execute(select(schema.runs.c.spec).where(schema.runs.c.run_id == run_id)).scalar_one_or_none()
        if not isinstance(spec, dict):
            return None
        orchestration = spec.get('orchestration')
        if not isinstance(orchestration, dict):
            return None
        raw = orchestration.get('concurrency_limit')
        if raw is None:
            return None
        limit = int(raw)
        return limit if limit > 0 else None

    def _mark_newly_ready_agents_in_transaction(self, conn: Any, run_id: str) -> list[str]:
        rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
        completed = {row['name'] for row in rows if row['status'] == 'completed'}
        ready = [
            row['name']
            for row in rows
            if row['status'] == 'pending' and all(dep in completed for dep in (row['depends_on'] or []))
        ]
        now = datetime.now(timezone.utc)
        for name in ready:
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                .values(status='queued', updated_at=now)
            )
            _ = self.append_event_in_transaction(conn, run_id, name, 'agent_queued', {'reason': 'dependencies_completed'})
        return ready

    def mark_newly_ready_agents(self, run_id: str) -> list[str]:
        """Move pending agents whose dependencies completed to queued."""

        with self.engine.begin() as conn:
            _ = self._mark_newly_ready_agents_in_transaction(conn, run_id)
            return self._claimable_ready_agents_in_connection(conn, run_id)

    def claim_agent(self, run_id: str, agent_name: str, *, worker_id: str, lease_seconds: int = 300) -> ClaimedAgent | None:
        """Atomically claim a queued agent for a worker."""

        lease_token = uuid4().hex
        attempt_id = uuid4().hex
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(
                select(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name))
                .with_for_update()
            ).mappings().first()
            if row is None or row['status'] != 'queued':
                return None
            if agent_name not in set(self._claimable_ready_agents_in_connection(conn, run_id)):
                return None
            attempt_number = int(
                conn.execute(
                    select(func.coalesce(func.max(schema.agent_attempts.c.attempt_number), 0)).where(
                        and_(
                            schema.agent_attempts.c.run_id == run_id,
                            schema.agent_attempts.c.agent == agent_name,
                        )
                    )
                ).scalar_one()
            ) + 1
            result = conn.execute(
                update(schema.agents)
                .where(
                    and_(
                        schema.agents.c.run_id == run_id,
                        schema.agents.c.name == agent_name,
                        schema.agents.c.status == 'queued',
                    )
                )
                .values(
                    status='running',
                    worker_id=worker_id,
                    lease_token=lease_token,
                    leased_until=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            conn.execute(
                insert(schema.agent_attempts).values(
                    id=attempt_id,
                    run_id=run_id,
                    agent=agent_name,
                    attempt_number=attempt_number,
                    runtime=row['runtime'],
                    model=row['model'],
                    status='running',
                    worker_id=worker_id,
                    lease_token=lease_token,
                    leased_until=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    started_at=now,
                )
            )
            _ = self.append_event_in_transaction(
                conn,
                run_id,
                agent_name,
                'agent_claimed',
                {'worker_id': worker_id, 'lease_seconds': lease_seconds},
            )
            conn.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == run_id)
                .values(status='running', updated_at=now)
            )
            return ClaimedAgent(
                run_id=run_id,
                name=agent_name,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                type=row['type'],
                runtime=row['runtime'],
                model=row['model'],
                branch=row['branch'],
                lease_token=lease_token,
                worker_id=worker_id,
            )

    def renew_lease(self, run_id: str, agent_name: str, *, worker_id: str, lease_token: str, lease_seconds: int = 300) -> bool:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            result = conn.execute(
                update(schema.agents)
                .where(
                    and_(
                        schema.agents.c.run_id == run_id,
                        schema.agents.c.name == agent_name,
                        schema.agents.c.worker_id == worker_id,
                        schema.agents.c.lease_token == lease_token,
                        schema.agents.c.status == 'running',
                    )
                )
                .values(leased_until=now + timedelta(seconds=lease_seconds), heartbeat_at=now, updated_at=now)
            )
            if result.rowcount == 1:
                conn.execute(
                    update(schema.agent_attempts)
                    .where(
                        and_(
                            schema.agent_attempts.c.run_id == run_id,
                            schema.agent_attempts.c.agent == agent_name,
                            schema.agent_attempts.c.worker_id == worker_id,
                            schema.agent_attempts.c.lease_token == lease_token,
                            schema.agent_attempts.c.status == 'running',
                        )
                    )
                    .values(
                        leased_until=now + timedelta(seconds=lease_seconds),
                        heartbeat_at=now,
                        updated_at=now,
                    )
                )
            return result.rowcount == 1

    def complete_agent(self, run_id: str, agent_name: str, *, cost_usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0, attempt_id: str | None = None) -> list[str]:
        """Mark an agent complete and queue newly unblocked dependents."""

        now = datetime.now(timezone.utc)
        ready: list[str] = []
        with self.engine.begin() as conn:
            attempt = self._running_attempt(conn, run_id, agent_name, attempt_id)
            update_conditions = [
                schema.agents.c.run_id == run_id,
                schema.agents.c.name == agent_name,
                schema.agents.c.status == 'running' if attempt_id else schema.agents.c.status.not_in(TERMINAL_AGENT_STATUSES),
            ]
            if attempt is not None:
                update_conditions.extend(
                    [
                        schema.agents.c.worker_id == attempt['worker_id'],
                        schema.agents.c.lease_token == attempt['lease_token'],
                    ]
                )
            elif attempt_id is not None:
                return []
            result = conn.execute(
                update(schema.agents)
                .where(and_(*update_conditions))
                .values(
                    status='completed',
                    cost_usd=cost_usd,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    lease_token=None,
                    leased_until=None,
                    worker_id=None,
                    heartbeat_at=None,
                    error=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return []
            if attempt_id:
                conn.execute(
                    update(schema.agent_attempts)
                    .where(and_(schema.agent_attempts.c.id == attempt_id, schema.agent_attempts.c.status == 'running'))
                    .values(status='completed', finished_at=now, updated_at=now)
            )
            _ = self.append_event_in_transaction(conn, run_id, agent_name, 'done', {'cost_usd': cost_usd})
            _ = self._mark_newly_ready_agents_in_transaction(conn, run_id)
            ready = self._claimable_ready_agents_in_connection(conn, run_id)
            budget_action = self._enforce_run_budget_in_transaction(conn, run_id, now)
            if budget_action in {'pause', 'cancel'}:
                ready = []
        self.refresh_run_status(run_id)
        return ready

    def _running_attempt(self, conn: Any, run_id: str, agent_name: str, attempt_id: str | None) -> dict[str, Any] | None:
        if attempt_id is None:
            return None
        row = conn.execute(
            select(schema.agent_attempts).where(
                and_(
                    schema.agent_attempts.c.id == attempt_id,
                    schema.agent_attempts.c.run_id == run_id,
                    schema.agent_attempts.c.agent == agent_name,
                    schema.agent_attempts.c.status == 'running',
                )
            )
        ).mappings().first()
        return dict(row) if row else None

    def _fail_dependency_blocked_agents_in_transaction(self, conn: Any, run_id: str, failed_agent: str, now: datetime) -> None:
        failed = {failed_agent}
        while True:
            rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
            newly_failed = [
                row['name']
                for row in rows
                if row['status'] in {'pending', 'queued'} and any(dep in failed for dep in (row['depends_on'] or []))
            ]
            if not newly_failed:
                return
            for name in newly_failed:
                failed.add(name)
                message = f'Dependency failed: {failed_agent}'
                conn.execute(
                    update(schema.agents)
                    .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                    .values(
                        status='failed',
                        error=message,
                        last_error=message,
                        lease_token=None,
                        leased_until=None,
                        worker_id=None,
                        heartbeat_at=None,
                        updated_at=now,
                    )
                )
                _ = self.append_event_in_transaction(conn, run_id, name, 'dependency_failed', {'dependency': failed_agent})

    def _stop_run_after_agent_failure_in_transaction(self, conn: Any, run_id: str, failed_agent: str, error: str, now: datetime) -> None:
        rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
        stopped = [
            row['name']
            for row in rows
            if row['name'] != failed_agent and row['status'] not in TERMINAL_AGENT_STATUSES
        ]
        for name in stopped:
            message = f'Stopped after failure of {failed_agent}'
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                .values(
                    status='cancelled',
                    error=message,
                    lease_token=None,
                    leased_until=None,
                    worker_id=None,
                    heartbeat_at=None,
                    updated_at=now,
                )
            )
            _ = self.append_event_in_transaction(conn, run_id, name, 'agent_cancelled', {'error': message})
        if stopped:
            conn.execute(
                update(schema.agent_attempts)
                .where(
                    and_(
                        schema.agent_attempts.c.run_id == run_id,
                        schema.agent_attempts.c.agent.in_(stopped),
                        schema.agent_attempts.c.status == 'running',
                    )
                )
                .values(status='cancelled', finished_at=now, updated_at=now)
            )
        conn.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run_id)
            .values(status='failed', updated_at=now)
        )
        _ = self.append_event_in_transaction(conn, run_id, '_system', 'run_stopped', {'agent': failed_agent, 'error': error[:1000]})

    def _pause_pending_work_in_transaction(self, conn: Any, run_id: str, now: datetime, *, reason: str) -> list[str]:
        rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
        paused = [row['name'] for row in rows if row['status'] in {'pending', 'queued'}]
        for name in paused:
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                .values(status='paused', error=reason, updated_at=now)
            )
            _ = self.append_event_in_transaction(conn, run_id, name, 'agent_paused', {'reason': reason})
        if paused:
            conn.execute(update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='paused', updated_at=now))
        return paused

    def _cancel_non_terminal_work_in_transaction(self, conn: Any, run_id: str, now: datetime, *, reason: str) -> list[str]:
        rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
        cancelled = [row['name'] for row in rows if row['status'] not in TERMINAL_AGENT_STATUSES]
        for name in cancelled:
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                .values(
                    status='cancelled',
                    error=reason,
                    lease_token=None,
                    leased_until=None,
                    worker_id=None,
                    heartbeat_at=None,
                    updated_at=now,
                )
            )
            _ = self.append_event_in_transaction(conn, run_id, name, 'agent_cancelled', {'error': reason})
        if cancelled:
            conn.execute(
                update(schema.agent_attempts)
                .where(
                    and_(
                        schema.agent_attempts.c.run_id == run_id,
                        schema.agent_attempts.c.agent.in_(cancelled),
                        schema.agent_attempts.c.status == 'running',
                    )
                )
                .values(status='cancelled', finished_at=now, updated_at=now)
            )
        return cancelled

    def _enforce_run_budget_in_transaction(self, conn: Any, run_id: str, now: datetime) -> str | None:
        run = conn.execute(select(schema.runs).where(schema.runs.c.run_id == run_id)).mappings().first()
        if run is None:
            return None
        total_cost = float(
            conn.execute(
                select(func.coalesce(func.sum(schema.agents.c.cost_usd), 0.0)).where(schema.agents.c.run_id == run_id)
            ).scalar_one()
        )
        max_cost = float(run['max_cost_usd'] or 0.0)
        if max_cost <= 0 or total_cost <= max_cost:
            return None
        spec = run['spec'] or {}
        budget = spec.get('cost_budget') if isinstance(spec, dict) else None
        action = str((budget or {}).get('on_exceed') or 'pause')
        _ = self.append_event_in_transaction(
            conn,
            run_id,
            '_system',
            'cost_budget_exceeded',
            {'total_cost_usd': total_cost, 'max_cost_usd': max_cost, 'action': action},
        )
        if action == 'warn':
            return 'warn'
        if action == 'cancel':
            self._cancel_non_terminal_work_in_transaction(conn, run_id, now, reason='Run cost budget exceeded')
            conn.execute(update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='cost_exceeded', total_cost_usd=total_cost, updated_at=now))
            return 'cancel'
        self._pause_pending_work_in_transaction(conn, run_id, now, reason='Run cost budget exceeded')
        conn.execute(update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='paused', total_cost_usd=total_cost, updated_at=now))
        return 'pause'

    def _enforce_circuit_breaker_in_transaction(self, conn: Any, run_id: str, now: datetime) -> str | None:
        run = conn.execute(select(schema.runs).where(schema.runs.c.run_id == run_id)).mappings().first()
        if run is None:
            return None
        spec = run['spec'] or {}
        orchestration = spec.get('orchestration') if isinstance(spec, dict) else None
        breaker = (orchestration or {}).get('circuit_breaker') if isinstance(orchestration, dict) else None
        if not breaker:
            return None
        threshold = int(breaker.get('threshold') or 0)
        if threshold <= 0:
            return None
        failure_count = int(
            conn.execute(
                select(func.count()).where(
                    and_(
                        schema.agents.c.run_id == run_id,
                        schema.agents.c.status.in_(FAILURE_AGENT_STATUSES),
                    )
                )
            ).scalar_one()
        )
        if failure_count < threshold:
            return None
        action = str(breaker.get('action') or 'cancel_all')
        _ = self.append_event_in_transaction(
            conn,
            run_id,
            '_system',
            'circuit_breaker_tripped',
            {'failure_count': failure_count, 'threshold': threshold, 'action': action},
        )
        if action == 'notify_only':
            return 'notify_only'
        if action == 'pause':
            self._pause_pending_work_in_transaction(conn, run_id, now, reason='Circuit breaker tripped')
            return 'pause'
        self._cancel_non_terminal_work_in_transaction(conn, run_id, now, reason='Circuit breaker tripped')
        conn.execute(update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='failed', updated_at=now))
        return 'cancel_all'

    def fail_agent(
        self,
        run_id: str,
        agent_name: str,
        error: str,
        *,
        attempt_id: str | None = None,
        error_id: str | None = None,
        retryable: bool | None = None,
        terminal_status: str = 'failed',
    ) -> list[str]:
        now = datetime.now(timezone.utc)
        queued: list[str] = []
        if terminal_status not in {'failed', 'timeout', 'cost_exceeded'}:
            terminal_status = 'failed'
        with self.engine.begin() as conn:
            row = conn.execute(
                select(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name))
                .with_for_update()
            ).mappings().first()
            if row is None or row['status'] in TERMINAL_AGENT_STATUSES:
                return []
            attempt = self._running_attempt(conn, run_id, agent_name, attempt_id)
            update_conditions = [
                schema.agents.c.run_id == run_id,
                schema.agents.c.name == agent_name,
                schema.agents.c.status == 'running' if attempt_id else schema.agents.c.status.not_in(TERMINAL_AGENT_STATUSES),
            ]
            if attempt is not None:
                update_conditions.extend(
                    [
                        schema.agents.c.worker_id == attempt['worker_id'],
                        schema.agents.c.lease_token == attempt['lease_token'],
                    ]
                )
            elif attempt_id is not None:
                return []
            retry_budget_remaining = int(row['retry_attempt'] or 0) < int(row['retry_count'] or 0)
            can_retry = (
                terminal_status != 'cost_exceeded'
                and row['on_failure'] == 'retry'
                and retryable is not False
                and retry_budget_remaining
            )
            next_status = 'queued' if can_retry else terminal_status
            values: dict[str, Any] = {
                'status': next_status,
                'lease_token': None,
                'leased_until': None,
                'worker_id': None,
                'heartbeat_at': None,
                'last_error': error[:1000],
                'updated_at': now,
            }
            if can_retry:
                values['retry_attempt'] = int(row['retry_attempt'] or 0) + 1
                values['error'] = None
            else:
                values['error'] = error[:1000]
            result = conn.execute(update(schema.agents).where(and_(*update_conditions)).values(**values))
            if result.rowcount != 1:
                return []
            if attempt_id:
                conn.execute(
                    update(schema.agent_attempts)
                    .where(and_(schema.agent_attempts.c.id == attempt_id, schema.agent_attempts.c.status == 'running'))
                    .values(status='failed', finished_at=now, error_id=error_id, updated_at=now)
                )
            _ = self.append_event_in_transaction(
                conn,
                run_id,
                agent_name,
                'error',
                {'error': error[:1000], 'retryable': retryable, 'retry_queued': can_retry, 'status': next_status},
            )
            if can_retry:
                queued.append(agent_name)
                _ = self.append_event_in_transaction(
                    conn,
                    run_id,
                    agent_name,
                    'agent_queued',
                    {'reason': 'retry', 'retry_attempt': values['retry_attempt']},
                )
            else:
                breaker_action = self._enforce_circuit_breaker_in_transaction(conn, run_id, now)
                if breaker_action in {'cancel_all', 'pause'}:
                    queued = []
                elif row['on_failure'] == 'stop':
                    self._stop_run_after_agent_failure_in_transaction(conn, run_id, agent_name, error, now)
                else:
                    self._fail_dependency_blocked_agents_in_transaction(conn, run_id, agent_name, now)
                    queued.extend(self._mark_newly_ready_agents_in_transaction(conn, run_id))
        self.refresh_run_status(run_id)
        return queued

    def cancel_run(self, run_id: str) -> int:
        """Cancel a run and all non-terminal agents. Returns affected agents."""

        terminal = {'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'}
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            run = conn.execute(select(schema.runs.c.status).where(schema.runs.c.run_id == run_id)).mappings().first()
            if run is None or run['status'] in terminal:
                return 0
            conn.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == run_id)
                .values(status='cancelled', cancelled_at=now, updated_at=now)
            )
            rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
            names = [row['name'] for row in rows if row['status'] not in terminal]
            for name in names:
                conn.execute(
                    update(schema.agents)
                    .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == name))
                    .values(
                        status='cancelled',
                        error='Run cancelled',
                        lease_token=None,
                        leased_until=None,
                        worker_id=None,
                        heartbeat_at=None,
                        updated_at=now,
                    )
                )
            if names:
                conn.execute(
                    update(schema.agent_attempts)
                    .where(
                        and_(
                            schema.agent_attempts.c.run_id == run_id,
                            schema.agent_attempts.c.agent.in_(names),
                            schema.agent_attempts.c.status == 'running',
                        )
                    )
                    .values(status='cancelled', finished_at=now, updated_at=now)
                )
            _ = self.append_event_in_transaction(conn, run_id, '_system', 'run_cancelled', {'cancelled_agents': names})
            return len(names)

    def refresh_run_status(self, run_id: str) -> str | None:
        """Recompute aggregate run status from canonical agent rows."""

        with self.engine.begin() as conn:
            run = conn.execute(select(schema.runs).where(schema.runs.c.run_id == run_id)).mappings().first()
            if run is None:
                return None
            if run['status'] in {'cancelled', 'cost_exceeded'}:
                return str(run['status'])
            rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
            statuses = [row['status'] for row in rows]
            if not statuses:
                next_status = str(run['status'])
            elif all(status == 'completed' for status in statuses):
                next_status = 'completed'
            elif all(status in TERMINAL_AGENT_STATUSES for status in statuses):
                if any(status == 'cost_exceeded' for status in statuses):
                    next_status = 'cost_exceeded'
                elif any(status in {'failed', 'timeout'} for status in statuses):
                    next_status = 'failed'
                else:
                    next_status = 'cancelled'
            elif any(status == 'running' for status in statuses):
                next_status = 'running'
            elif any(status == 'paused' for status in statuses):
                next_status = 'paused'
            else:
                next_status = 'queued'
            total_cost = float(
                conn.execute(
                    select(func.coalesce(func.sum(schema.agents.c.cost_usd), 0.0)).where(schema.agents.c.run_id == run_id)
                ).scalar_one()
            )
            conn.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == run_id)
                .values(status=next_status, total_cost_usd=total_cost, updated_at=datetime.now(timezone.utc))
            )
            return next_status

    def resume_run(self, run_id: str) -> list[dict[str, Any]]:
        """Requeue agents that are eligible for a deployed retry/resume."""

        now = datetime.now(timezone.utc)
        resumed: list[dict[str, Any]] = []
        with self.engine.begin() as conn:
            rows = conn.execute(select(schema.agents).where(schema.agents.c.run_id == run_id)).mappings().all()
            completed = {row['name'] for row in rows if row['status'] == 'completed'}
            for row in rows:
                retryable_failure = row['status'] in {'failed', 'timeout'} and row['on_failure'] == 'retry' and int(row['retry_attempt'] or 0) < int(row['retry_count'] or 0)
                paused = row['status'] == 'paused'
                if not retryable_failure and not paused:
                    continue
                next_status = 'queued' if all(dep in completed for dep in (row['depends_on'] or [])) else 'pending'
                values = {
                    'status': next_status,
                    'error': None,
                    'lease_token': None,
                    'leased_until': None,
                    'worker_id': None,
                    'heartbeat_at': None,
                    'updated_at': now,
                }
                if retryable_failure:
                    values['retry_attempt'] = int(row['retry_attempt'] or 0) + 1
                conn.execute(
                    update(schema.agents)
                    .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == row['name']))
                    .values(**values)
                )
                _ = self.append_event_in_transaction(conn, run_id, row['name'], 'agent_resumed', {'status': next_status})
                resumed.append({'run_id': run_id, 'agent': row['name'], 'status': next_status})
            if resumed:
                conn.execute(update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='queued', updated_at=now))
        if resumed:
            self.mark_newly_ready_agents(run_id)
            self.refresh_run_status(run_id)
        return resumed

    def update_agent_worktree(self, run_id: str, agent_name: str, *, worktree_locator: str, branch: str | None = None) -> None:
        values: dict[str, Any] = {
            'worktree_locator': worktree_locator,
            'updated_at': datetime.now(timezone.utc),
        }
        if branch is not None:
            values['branch'] = branch
        with self.engine.begin() as conn:
            conn.execute(
                update(schema.agents)
                .where(and_(schema.agents.c.run_id == run_id, schema.agents.c.name == agent_name))
                .values(**values)
            )

    def record_worker_heartbeat(self, worker_id: str, *, hostname: str | None = None, version: str | None = None, capacity: dict[str, Any] | None = None, status: str = 'active') -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(schema.worker_nodes.c.worker_id).where(schema.worker_nodes.c.worker_id == worker_id)
            ).scalar_one_or_none()
            values = {
                'hostname': hostname,
                'version': version,
                'heartbeat_at': now,
                'capacity': redact_attributes(capacity or {}),
                'status': status,
            }
            if existing:
                conn.execute(update(schema.worker_nodes).where(schema.worker_nodes.c.worker_id == worker_id).values(**values))
            else:
                conn.execute(insert(schema.worker_nodes).values(worker_id=worker_id, started_at=now, **values))

    def list_worker_nodes(self, *, stale_after_seconds: int = 60) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self.engine.connect() as conn:
            rows = conn.execute(select(schema.worker_nodes).order_by(schema.worker_nodes.c.heartbeat_at.desc())).mappings().all()
            workers = []
            for row in rows:
                item = dict(row)
                heartbeat = item.get('heartbeat_at')
                if isinstance(heartbeat, str):
                    heartbeat = datetime.fromisoformat(heartbeat.replace('Z', '+00:00'))
                if heartbeat is not None and heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                stale = heartbeat is None or (now - heartbeat).total_seconds() > stale_after_seconds
                item['stale'] = stale
                workers.append(item)
            return workers

    def record_runtime_session(
        self,
        *,
        attempt_id: str,
        run_id: str,
        agent: str,
        provider: str,
        runtime: str,
        model: str | None = None,
        provider_session_id: str | None = None,
        provider_thread_id: str | None = None,
        cwd_locator: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        session_id = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.runtime_sessions).values(
                    id=session_id,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    agent=agent,
                    provider=provider,
                    runtime=runtime,
                    provider_session_id=provider_session_id,
                    provider_thread_id=provider_thread_id,
                    cwd_locator=cwd_locator,
                    model=model,
                    metadata=redact_attributes(metadata or {}),
                )
            )
        return session_id

    def update_runtime_session_provider_ids(
        self,
        session_id: str,
        *,
        provider_session_id: str | None = None,
        provider_thread_id: str | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if provider_session_id:
            values['provider_session_id'] = provider_session_id
        if provider_thread_id:
            values['provider_thread_id'] = provider_thread_id
        if not values:
            return
        with self.engine.begin() as conn:
            conn.execute(update(schema.runtime_sessions).where(schema.runtime_sessions.c.id == session_id).values(**values))

    def latest_provider_resume_ids(self, run_id: str, agent: str, provider: str) -> dict[str, str | None]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    schema.runtime_sessions.c.provider_session_id,
                    schema.runtime_sessions.c.provider_thread_id,
                )
                .where(
                    and_(
                        schema.runtime_sessions.c.run_id == run_id,
                        schema.runtime_sessions.c.agent == agent,
                        schema.runtime_sessions.c.provider == provider,
                        (
                            schema.runtime_sessions.c.provider_session_id.is_not(None)
                            | schema.runtime_sessions.c.provider_thread_id.is_not(None)
                        ),
                    )
                )
                .order_by(schema.runtime_sessions.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            if row is None:
                return {'provider_session_id': None, 'provider_thread_id': None}
            return dict(row)

    def get_attempts(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.agent_attempts).where(schema.agent_attempts.c.run_id == run_id).order_by(schema.agent_attempts.c.started_at)
            if agent:
                stmt = stmt.where(schema.agent_attempts.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def get_runtime_sessions(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.runtime_sessions).where(schema.runtime_sessions.c.run_id == run_id).order_by(schema.runtime_sessions.c.created_at)
            if agent:
                stmt = stmt.where(schema.runtime_sessions.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def get_runtime_invocations(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.runtime_invocations).where(schema.runtime_invocations.c.run_id == run_id).order_by(schema.runtime_invocations.c.started_at)
            if agent:
                stmt = stmt.where(schema.runtime_invocations.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def get_runtime_errors(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.runtime_errors).where(schema.runtime_errors.c.run_id == run_id).order_by(schema.runtime_errors.c.created_at)
            if agent:
                stmt = stmt.where(schema.runtime_errors.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def get_token_usage(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.token_usage).where(schema.token_usage.c.run_id == run_id).order_by(schema.token_usage.c.created_at)
            if agent:
                stmt = stmt.where(schema.token_usage.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def get_cost_usage(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.cost_usage).where(schema.cost_usage.c.run_id == run_id).order_by(schema.cost_usage.c.created_at)
            if agent:
                stmt = stmt.where(schema.cost_usage.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def start_runtime_invocation(
        self,
        *,
        attempt_id: str,
        run_id: str,
        agent: str,
        kind: str,
        session_id: str | None = None,
        provider_turn_id: str | None = None,
    ) -> str:
        invocation_id = uuid4().hex
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            scope_column = schema.runtime_invocations.c.session_id if session_id else schema.runtime_invocations.c.attempt_id
            scope_value = session_id or attempt_id
            sequence = int(
                conn.execute(
                    select(func.coalesce(func.max(schema.runtime_invocations.c.sequence), 0)).where(scope_column == scope_value)
                ).scalar_one()
            ) + 1
            conn.execute(
                insert(schema.runtime_invocations).values(
                    id=invocation_id,
                    session_id=session_id,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    agent=agent,
                    provider_turn_id=provider_turn_id,
                    sequence=sequence,
                    kind=kind,
                    status='running',
                    started_at=now,
                    is_error=False,
                )
            )
        return invocation_id

    def finish_runtime_invocation(
        self,
        invocation_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        stop_reason: str | None = None,
        final_message_artifact_id: str | None = None,
        final_message_hash: str | None = None,
        error_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(
                select(schema.runtime_invocations.c.started_at).where(schema.runtime_invocations.c.id == invocation_id)
            ).mappings().first()
            started_at = row['started_at'] if row else now
            if isinstance(started_at, str):
                started_at = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
            conn.execute(
                update(schema.runtime_invocations)
                .where(schema.runtime_invocations.c.id == invocation_id)
                .values(
                    status=status,
                    completed_at=now,
                    duration_ms=duration_ms,
                    exit_code=exit_code,
                    stop_reason=stop_reason,
                    is_error=status not in {'ok', 'completed', 'success'},
                    error_id=error_id,
                    final_message_artifact_id=final_message_artifact_id,
                    final_message_hash=final_message_hash,
                )
            )

    def record_runtime_error(
        self,
        *,
        run_id: str,
        agent: str | None,
        source: str,
        message: str,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
        code: str | None = None,
        details_artifact_id: str | None = None,
        retryable: bool | None = None,
    ) -> str:
        error_id = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.runtime_errors).values(
                    id=error_id,
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    source=source,
                    code=code,
                    message_hash=stable_hash(message),
                    message_preview=redact_freeform_text(message[:1000]),
                    details_artifact_id=details_artifact_id,
                    retryable=retryable,
                )
            )
            return error_id

    def record_runtime_mcp_server(
        self,
        *,
        session_id: str,
        name: str,
        status: str,
        scope: str | None = None,
        config: dict[str, Any] | None = None,
        server_name: str | None = None,
        server_version: str | None = None,
        error_id: str | None = None,
    ) -> str:
        """Record a configured external MCP server for a runtime session."""

        server_id = uuid4().hex
        safe_config = redact_attributes(config or {})
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.runtime_mcp_servers).values(
                    id=server_id,
                    session_id=session_id,
                    name=name,
                    status=status,
                    server_name=server_name,
                    server_version=server_version,
                    scope=scope,
                    config_hash=canonical_json_hash(safe_config),
                    error_id=error_id,
                )
            )
        return server_id

    def record_token_usage(
        self,
        *,
        run_id: str,
        agent: str | None,
        provider: str,
        scope: str,
        model: str | None = None,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        context_window: int | None = None,
        raw_usage: dict[str, Any] | None = None,
    ) -> None:
        total_tokens = input_tokens + output_tokens + cached_input_tokens + reasoning_output_tokens
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.token_usage).values(
                    id=uuid4().hex,
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    provider=provider,
                    model=model,
                    scope=scope,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                    reasoning_output_tokens=reasoning_output_tokens,
                    total_tokens=total_tokens,
                    context_window=context_window,
                    raw_usage=redact_attributes(raw_usage or {}),
                )
            )

    def record_cost_usage(
        self,
        *,
        run_id: str,
        agent: str | None,
        provider: str,
        amount_usd: float,
        source: str,
        model: str | None = None,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
        raw_cost: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.cost_usage).values(
                    id=uuid4().hex,
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    provider=provider,
                    model=model,
                    amount_usd=amount_usd,
                    source=source,
                    raw_cost=redact_attributes(raw_cost or {}),
                )
            )

    def record_provider_event(
        self,
        *,
        run_id: str,
        agent: str | None,
        provider: str,
        runtime: str,
        event_name: str,
        sequence: int,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
        provider_event_id: str | None = None,
        provider_thread_id: str | None = None,
        provider_turn_id: str | None = None,
        provider_message_id: str | None = None,
        payload_schema: str | None = None,
        payload_version: str | None = None,
        payload_preview: dict[str, Any] | None = None,
        payload_artifact_id: str | None = None,
    ) -> str:
        event_id = uuid4().hex
        payload = redact_attributes(payload_preview or {})
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.provider_events).values(
                    id=event_id,
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    provider=provider,
                    runtime=runtime,
                    event_name=event_name,
                    provider_event_id=provider_event_id,
                    provider_thread_id=provider_thread_id,
                    provider_turn_id=provider_turn_id,
                    provider_message_id=provider_message_id,
                    sequence=sequence,
                    payload_schema=payload_schema,
                    payload_version=payload_version,
                    payload_hash=canonical_json_hash(payload),
                    payload_preview=payload,
                    payload_artifact_id=payload_artifact_id,
                )
            )
        return event_id

    def record_queue_outbox(self, run_id: str, agent: str | None, event_type: str, payload: dict[str, Any]) -> str:
        outbox_id = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.queue_outbox).values(
                    id=outbox_id,
                    run_id=run_id,
                    agent=agent,
                    event_type=event_type,
                    payload=redact_attributes(payload),
                    status='pending',
                )
            )
        return outbox_id

    def mark_outbox_published(self, outbox_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(schema.queue_outbox)
                .where(schema.queue_outbox.c.id == outbox_id)
                .values(status='published', published_at=datetime.now(timezone.utc))
            )

    def pending_queue_outbox(self, *, limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.queue_outbox)
                .where(
                    and_(
                        schema.queue_outbox.c.status == 'pending',
                        (schema.queue_outbox.c.next_attempt_at.is_(None) | (schema.queue_outbox.c.next_attempt_at <= now)),
                    )
                )
                .order_by(schema.queue_outbox.c.created_at)
                .limit(limit)
            ).mappings().all()
            return [dict(row) for row in rows]

    def mark_outbox_retry(self, outbox_id: str, error: str, *, delay_seconds: int = 30) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(select(schema.queue_outbox).where(schema.queue_outbox.c.id == outbox_id)).mappings().first()
            attempts = int(row['attempts'] or 0) + 1 if row else 1
            conn.execute(
                update(schema.queue_outbox)
                .where(schema.queue_outbox.c.id == outbox_id)
                .values(
                    attempts=attempts,
                    next_attempt_at=now + timedelta(seconds=delay_seconds),
                    payload=redact_attributes({**dict(row['payload'] or {}), 'last_error': error[:1000]}) if row else {},
                )
            )

    def expire_stale_leases(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Expire running agents whose Postgres lease is stale.

        Returns agents requeued by the expiration pass.
        """

        now = now or datetime.now(timezone.utc)
        requeued: list[dict[str, Any]] = []
        affected_run_ids: set[str] = set()
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(schema.agents).where(
                    and_(
                        schema.agents.c.status == 'running',
                        schema.agents.c.leased_until.is_not(None),
                        schema.agents.c.leased_until < now,
                    )
                )
            ).mappings().all()
            for row in rows:
                can_retry = row['on_failure'] == 'retry' and int(row['retry_attempt'] or 0) < int(row['retry_count'] or 0)
                new_status = 'queued' if can_retry else 'failed'
                values = {
                    'status': new_status,
                    'lease_token': None,
                    'leased_until': None,
                    'worker_id': None,
                    'heartbeat_at': None,
                    'updated_at': now,
                }
                if can_retry:
                    values['retry_attempt'] = int(row['retry_attempt'] or 0) + 1
                    values['last_error'] = 'Lease expired'
                else:
                    values['error'] = 'Lease expired'
                    values['last_error'] = 'Lease expired'
                conn.execute(
                    update(schema.agents)
                    .where(and_(schema.agents.c.run_id == row['run_id'], schema.agents.c.name == row['name']))
                    .values(**values)
                )
                attempt = conn.execute(
                    select(schema.agent_attempts)
                    .where(
                        and_(
                            schema.agent_attempts.c.run_id == row['run_id'],
                            schema.agent_attempts.c.agent == row['name'],
                            schema.agent_attempts.c.status == 'running',
                        )
                    )
                    .order_by(schema.agent_attempts.c.attempt_number.desc())
                    .limit(1)
                ).mappings().first()
                if attempt:
                    conn.execute(
                        update(schema.agent_attempts)
                        .where(schema.agent_attempts.c.id == attempt['id'])
                        .values(status='expired', finished_at=now, updated_at=now)
                    )
                _ = self.append_event_in_transaction(conn, row['run_id'], row['name'], 'lease_expired', {'requeued': can_retry})
                if not can_retry:
                    if row['on_failure'] == 'stop':
                        self._stop_run_after_agent_failure_in_transaction(conn, row['run_id'], row['name'], 'Lease expired', now)
                    else:
                        self._fail_dependency_blocked_agents_in_transaction(conn, row['run_id'], row['name'], now)
                        requeued.extend({'run_id': row['run_id'], 'agent': name} for name in self._mark_newly_ready_agents_in_transaction(conn, row['run_id']))
                affected_run_ids.add(str(row['run_id']))
                if can_retry:
                    requeued.append({'run_id': row['run_id'], 'agent': row['name']})
        for run_id in affected_run_ids:
            self.refresh_run_status(run_id)
        return requeued

    def append_event(
        self,
        run_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Append a redacted event in its own transaction."""

        with self.engine.begin() as conn:
            return self.append_event_in_transaction(conn, run_id, agent, event_type, data)

    def append_event_in_transaction(
        self,
        conn: Any,
        run_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Append a redacted event using an existing transaction."""
        event_id = uuid4().hex
        conn.execute(
            insert(schema.events).values(
                id=event_id,
                run_id=run_id,
                agent=agent,
                event_type=event_type,
                data=redact_attributes(data or {}),
            )
        )
        return event_id

    def record_trace_span(
        self,
        *,
        run_id: str,
        agent: str | None,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        status: str,
        started_at: datetime,
        ended_at: datetime,
        attributes: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
        export_status: str = 'mirrored',
    ) -> None:
        duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.trace_spans).values(
                    run_id=run_id,
                    agent=agent,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    name=name,
                    status=status,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    attributes=redact_attributes(attributes),
                    events=[redact_attributes(event) for event in events or []],
                    export_status=export_status,
                )
            )

    def fetch_trace_spans(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.trace_spans).where(schema.trace_spans.c.run_id == run_id).order_by(schema.trace_spans.c.started_at)
            if agent:
                stmt = stmt.where(schema.trace_spans.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def record_artifact(
        self,
        *,
        run_id: str,
        agent: str | None,
        kind: str,
        uri: str,
        sha256: str,
        size_bytes: int,
        redaction_policy: str,
        content_type: str,
        attempt_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
    ) -> str:
        artifact_id = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.artifacts).values(
                    id=artifact_id,
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    kind=kind,
                    uri=uri,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    redaction_policy=redaction_policy,
                    content_type=content_type,
                )
            )
        return artifact_id

    def get_artifacts(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.artifacts).where(schema.artifacts.c.run_id == run_id).order_by(schema.artifacts.c.created_at)
            if agent:
                stmt = stmt.where(schema.artifacts.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def record_check(
        self,
        *,
        run_id: str,
        agent: str,
        command: str,
        exit_code: int,
        duration_ms: int,
        output_artifact_id: str | None = None,
        attempt_id: str | None = None,
        runtime_invocation_id: str | None = None,
        shell: str | None = None,
        cwd_locator: str | None = None,
        env_metadata: dict[str, Any] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.checks).values(
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    runtime_invocation_id=runtime_invocation_id,
                    command_hash=stable_hash(command),
                    command_preview=redact_freeform_text(command[:500]),
                    shell=shell,
                    cwd_locator=cwd_locator,
                    env_metadata=redact_attributes(env_metadata or {}) if env_metadata is not None else None,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    output_artifact_id=output_artifact_id,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )

    def get_checks(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.checks).where(schema.checks.c.run_id == run_id).order_by(schema.checks.c.created_at)
            if agent:
                stmt = stmt.where(schema.checks.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def record_git_provenance(
        self,
        *,
        run_id: str,
        agent: str | None,
        diff_stats: dict[str, Any],
        attempt_id: str | None = None,
        base_ref: str | None = None,
        remote: str | None = None,
        worktree_locator: str | None = None,
        base_sha: str | None = None,
        merge_base_sha: str | None = None,
        head_sha: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        patch_artifact_id: str | None = None,
        commit_message: str | None = None,
        changed_files_count: int | None = None,
        insertions_count: int | None = None,
        deletions_count: int | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(schema.git_provenance).values(
                    run_id=run_id,
                    agent=agent,
                    attempt_id=attempt_id,
                    base_ref=base_ref,
                    remote=remote,
                    worktree_locator=worktree_locator,
                    base_sha=base_sha,
                    merge_base_sha=merge_base_sha,
                    head_sha=head_sha,
                    branch=branch,
                    commit_sha=commit_sha,
                    pr_url=pr_url,
                    pr_number=pr_number,
                    patch_artifact_id=patch_artifact_id,
                    commit_message_hash=stable_hash(commit_message) if commit_message else None,
                    commit_message_preview=redact_freeform_text(commit_message[:500]) if commit_message else None,
                    changed_files_count=changed_files_count,
                    insertions_count=insertions_count,
                    deletions_count=deletions_count,
                    diff_stats=redact_attributes(diff_stats),
                )
            )

    def get_git_provenance(self, run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            stmt = select(schema.git_provenance).where(schema.git_provenance.c.run_id == run_id).order_by(schema.git_provenance.c.created_at)
            if agent:
                stmt = stmt.where(schema.git_provenance.c.agent == agent)
            return [dict(row) for row in conn.execute(stmt).mappings().all()]

    def telemetry_summary(self, run_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            span_count = conn.execute(
                select(schema.trace_spans.c.id).where(schema.trace_spans.c.run_id == run_id)
            ).all()
            last_error = conn.execute(
                select(schema.events.c.data)
                .where(and_(schema.events.c.run_id == run_id, schema.events.c.event_type == 'telemetry_error'))
                .order_by(schema.events.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return {'trace_span_count': len(span_count), 'last_telemetry_error': last_error}
