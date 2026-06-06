"""Python client API for deployed spawnd."""
from __future__ import annotations

from typing import Literal

from spawnd.coordination.redis import CoordinationPlane
from spawnd.io.validation import validate_plan
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec, RuntimeName
from spawnd.state.repository import DeployedRepository
from spawnd.state.submission import submit_plan

__all__ = [
    "agent",
    "artifacts",
    "cancel",
    "checks",
    "events",
    "handoff",
    "pipeline",
    "provenance",
    "resume",
    "run",
    "status",
    "submit",
    "traces",
]


def agent(
    name: str,
    prompt: str,
    *,
    depends_on: list[str] | None = None,
    check: str | None = None,
    model: str | None = None,
    type: Literal["worker", "manager"] = "worker",
    use_role: str | None = None,
    max_iterations: int | None = None,
    max_cost_usd: float | None = None,
    on_failure: Literal["continue", "stop", "retry"] | None = None,
    retry_count: int | None = None,
    runtime: RuntimeName | None = None,
    env: dict[str, str] | None = None,
) -> AgentSpec:
    """Build an ``AgentSpec`` with Python-friendly keyword arguments."""

    kwargs: dict = {"name": name, "prompt": prompt, "type": type}
    if depends_on is not None:
        kwargs["depends_on"] = list(depends_on)
    if check is not None:
        kwargs["check"] = check
    if model is not None:
        kwargs["model"] = model
    if use_role is not None:
        kwargs["use_role"] = use_role
    if max_iterations is not None:
        kwargs["max_iterations"] = max_iterations
    if max_cost_usd is not None:
        kwargs["max_cost_usd"] = max_cost_usd
    if on_failure is not None:
        kwargs["on_failure"] = on_failure
    if retry_count is not None:
        kwargs["retry_count"] = retry_count
    if runtime is not None:
        kwargs["runtime"] = runtime
    if env is not None:
        kwargs["env"] = dict(env)
    return AgentSpec(**kwargs)


def _coerce_plan(
    agents: list[AgentSpec] | PlanSpec,
    *,
    name: str,
    defaults: Defaults | None,
    shared_context: list[str] | None,
) -> PlanSpec:
    if isinstance(agents, PlanSpec):
        return agents
    return PlanSpec(
        name=name,
        defaults=defaults or Defaults(),
        shared_context=list(shared_context) if shared_context else [],
        agents=list(agents),
    )


def submit(
    plan: PlanSpec,
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    run_id: str | None = None,
    source_repo: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Submit a run to the deployed backend."""

    errors = validate_plan(plan)
    if errors:
        raise ValueError(f"Invalid plan: {'; '.join(errors)}")
    return submit_plan(
        plan,
        repository=repository,
        coordinator=coordinator,
        run_id=run_id,
        source_repo=source_repo,
        source_ref=source_ref,
    )


def run(
    agents: list[AgentSpec] | PlanSpec,
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    name: str = "spawnd-run",
    run_id: str | None = None,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    source_repo: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Build and submit a deployed run."""

    plan = _coerce_plan(agents, name=name, defaults=defaults, shared_context=shared_context)
    return submit(plan, repository=repository, coordinator=coordinator, run_id=run_id, source_repo=source_repo, source_ref=source_ref)


def pipeline(
    steps: list[AgentSpec],
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    name: str = "spawnd-pipeline",
    run_id: str | None = None,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    source_repo: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Submit sequential agents by linking ``depends_on``."""

    chained: list[AgentSpec] = []
    for idx, step in enumerate(steps):
        if idx == 0:
            chained.append(step)
            continue
        prev_name = steps[idx - 1].name
        deps = list(step.depends_on)
        if prev_name not in deps:
            deps.append(prev_name)
        chained.append(step.model_copy(update={"depends_on": deps}))
    return run(
        chained,
        repository=repository,
        coordinator=coordinator,
        name=name,
        run_id=run_id,
        defaults=defaults,
        shared_context=shared_context,
        source_repo=source_repo,
        source_ref=source_ref,
    )


def handoff(
    a: AgentSpec,
    b: AgentSpec,
    *,
    repository: DeployedRepository,
    coordinator: CoordinationPlane,
    name: str = "spawnd-handoff",
    run_id: str | None = None,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    source_repo: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Submit two agents in sequence."""

    return pipeline(
        [a, b],
        repository=repository,
        coordinator=coordinator,
        name=name,
        run_id=run_id,
        defaults=defaults,
        shared_context=shared_context,
        source_repo=source_repo,
        source_ref=source_ref,
    )


def status(run_id: str, *, repository: DeployedRepository) -> dict:
    """Return run status reconstructed from Postgres."""

    run_row = repository.get_run(run_id)
    if run_row is None:
        raise ValueError(f"Run not found: {run_id}")
    return {
        "run": run_row,
        "agents": repository.get_agents(run_id),
        "attempts": repository.get_attempts(run_id),
        "telemetry": repository.telemetry_summary(run_id),
    }


def events(run_id: str, *, repository: DeployedRepository, limit: int = 100) -> list[dict]:
    return repository.get_events(run_id, limit=limit)


def artifacts(run_id: str, *, repository: DeployedRepository, agent_name: str | None = None) -> list[dict]:
    return repository.get_artifacts(run_id, agent_name)


def checks(run_id: str, *, repository: DeployedRepository, agent_name: str | None = None) -> list[dict]:
    return repository.get_checks(run_id, agent_name)


def traces(run_id: str, *, repository: DeployedRepository, agent_name: str | None = None) -> list[dict]:
    return repository.fetch_trace_spans(run_id, agent_name)


def provenance(run_id: str, *, repository: DeployedRepository, agent_name: str | None = None) -> list[dict]:
    return repository.get_git_provenance(run_id, agent_name)


def cancel(run_id: str, *, repository: DeployedRepository, coordinator: CoordinationPlane | None = None) -> int:
    """Cancel a run in Postgres, then publish a Redis cancellation hint."""

    cancelled = repository.cancel_run(run_id)
    if coordinator is not None:
        coordinator.publish_cancel(run_id)
    return cancelled


def resume(run_id: str, *, repository: DeployedRepository, coordinator: CoordinationPlane | None = None) -> list[dict]:
    """Resume retryable agents and publish queue hints for queued rows."""

    resumed = repository.resume_run(run_id)
    if coordinator is not None:
        for item in resumed:
            if item["status"] == "queued":
                outbox_id = repository.record_queue_outbox(run_id, item["agent"], "agent_ready", {"run_id": run_id, "agent": item["agent"]})
                coordinator.enqueue_agent(run_id, item["agent"])
                repository.mark_outbox_published(outbox_id)
    return resumed
