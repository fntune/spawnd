"""HTTP API for deployed spawnd."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from spawnd.api import cancel as cancel_run
from spawnd.api import resume as resume_run
from spawnd.config import load_backend_config
from spawnd.coordination.redis import RedisCoordinator
from spawnd.io.validation import validate_plan
from spawnd.models.specs import PlanSpec
from spawnd.state.repository import DeployedRepository
from spawnd.state.submission import submit_plan
from spawnd.workers.worker import reconcile_ready_agents


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    plan: PlanSpec | None = None
    run_id: str | None = None
    source_repo: str | None = None
    source_ref: str | None = None


def _repository() -> DeployedRepository:
    config = load_backend_config()
    if not config.database_url:
        raise HTTPException(status_code=500, detail="SPAWND_DATABASE_URL is required")
    return DeployedRepository.from_url(config.database_url)


def _coordinator() -> RedisCoordinator:
    config = load_backend_config()
    if not config.redis_url:
        raise HTTPException(status_code=500, detail="SPAWND_REDIS_URL is required")
    return RedisCoordinator.from_url(config.redis_url)


def _run_status(repo: DeployedRepository, run_id: str) -> dict[str, Any]:
    run_row = repo.get_run(run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {
        "run": run_row,
        "agents": repo.get_agents(run_id),
        "attempts": repo.get_attempts(run_id),
        "telemetry": repo.telemetry_summary(run_id),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="spawnd", version="0.1.0")

    @app.post("/runs")
    def submit(body: SubmitBody) -> dict[str, str]:
        plan = body.plan
        if plan is None:
            raise HTTPException(status_code=422, detail="plan is required")
        errors = validate_plan(plan)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        run_id = submit_plan(
            plan,
            repository=_repository(),
            coordinator=_coordinator(),
            run_id=body.run_id,
            source_repo=body.source_repo,
            source_ref=body.source_ref,
        )
        return {"run_id": run_id}

    @app.get("/runs/{run_id}")
    def status(run_id: str) -> dict[str, Any]:
        return _run_status(_repository(), run_id)

    @app.get("/runs/{run_id}/events")
    def events(run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return _repository().get_events(run_id, limit=limit)

    @app.get("/runs/{run_id}/checks")
    def checks(run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        return _repository().get_checks(run_id, agent)

    @app.get("/runs/{run_id}/artifacts")
    def artifacts(run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        return _repository().get_artifacts(run_id, agent)

    @app.get("/runs/{run_id}/traces")
    def traces(run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        return _repository().fetch_trace_spans(run_id, agent)

    @app.get("/runs/{run_id}/provenance")
    def provenance(run_id: str, agent: str | None = None) -> list[dict[str, Any]]:
        return _repository().get_git_provenance(run_id, agent)

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, int]:
        return {"cancelled": cancel_run(run_id, repository=_repository(), coordinator=_coordinator())}

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str) -> list[dict[str, Any]]:
        return resume_run(run_id, repository=_repository(), coordinator=_coordinator())

    @app.post("/workers/reconcile")
    def reconcile() -> list[dict[str, str]]:
        return reconcile_ready_agents(_repository(), _coordinator())

    return app
