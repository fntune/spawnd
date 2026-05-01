"""Python API for spawnd — peer to the CLI.

Everything here is a thin wrapper around :func:`spawnd.runtime.scheduler.run_plan`.
Runs started via this API are indistinguishable from runs started via the CLI:
same SQLite database under ``.spawnd/runs/<run_id>/``, same worktrees, same
``spawnd status``/``logs``/``merge``/``resume`` commands.
"""

from typing import Literal

from spawnd.io.validation import validate_plan
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
from spawnd.runtime.run_state import run_has_persisted_plan
from spawnd.runtime.scheduler import SchedulerResult, run_plan
from spawnd.storage.logs import setup_logging

__all__ = ["agent", "handoff", "pipeline", "run"]


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
    runtime: Literal["claude", "openai"] | None = None,
    env: dict[str, str] | None = None,
) -> AgentSpec:
    """Build an ``AgentSpec`` with Python-friendly keyword arguments.

    Omitted fields fall through to plan-level or global defaults.
    """
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


async def run(
    agents: list[AgentSpec] | PlanSpec,
    *,
    name: str = "spawnd-run",
    run_id: str | None = None,
    resume: bool = False,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    use_mock: bool = False,
    verbose: bool = False,
) -> SchedulerResult:
    """Execute agents through the scheduler.

    This is the single Python entry point. It mirrors ``spawnd run`` exactly:
    validates the plan, persists everything to SQLite, allocates git worktrees,
    and returns the :class:`SchedulerResult` the CLI would print.
    """
    if resume and not run_id:
        raise ValueError("resume=True requires run_id")
    if resume and run_id and not run_has_persisted_plan(run_id):
        raise ValueError(f"Run not found: {run_id}")

    plan = _coerce_plan(
        agents,
        name=name,
        defaults=defaults,
        shared_context=shared_context,
    )
    errors = validate_plan(plan)
    if errors:
        raise ValueError(f"Invalid plan: {'; '.join(errors)}")

    if run_id:
        setup_logging(run_id, verbose)
    return await run_plan(plan, run_id=run_id, use_mock=use_mock, resume=resume)


async def pipeline(
    steps: list[AgentSpec],
    *,
    name: str = "spawnd-pipeline",
    run_id: str | None = None,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    use_mock: bool = False,
    verbose: bool = False,
) -> SchedulerResult:
    """Run ``steps`` sequentially by auto-chaining ``depends_on``.

    Each step after the first is linked to the previous via ``depends_on``,
    causing the scheduler to run them in order with dependency context
    merged between worktrees. Existing ``depends_on`` values on steps are
    preserved (the previous step's name is appended, not replaced).
    """
    chained: list[AgentSpec] = []
    for idx, step in enumerate(steps):
        if idx == 0:
            chained.append(step)
            continue
        prev_name = steps[idx - 1].name
        existing = list(step.depends_on)
        if prev_name not in existing:
            existing.append(prev_name)
        chained.append(step.model_copy(update={"depends_on": existing}))

    return await run(
        chained,
        name=name,
        run_id=run_id,
        defaults=defaults,
        shared_context=shared_context,
        use_mock=use_mock,
        verbose=verbose,
    )


async def handoff(
    a: AgentSpec,
    b: AgentSpec,
    *,
    name: str = "spawnd-handoff",
    run_id: str | None = None,
    defaults: Defaults | None = None,
    shared_context: list[str] | None = None,
    use_mock: bool = False,
    verbose: bool = False,
) -> SchedulerResult:
    """Run ``a`` then ``b``; ``b`` inherits ``a``'s worktree via the dep merge."""
    return await pipeline(
        [a, b],
        name=name,
        run_id=run_id,
        defaults=defaults,
        shared_context=shared_context,
        use_mock=use_mock,
        verbose=verbose,
    )
