"""HTTP API for deployed spawnd."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict

from spawnd.api import cancel as cancel_run
from spawnd.api import resume as resume_run
from spawnd.config import load_backend_config
from spawnd.coordination.redis import RedisCoordinator
from spawnd.io.validation import validate_plan
from spawnd.models.specs import PlanSpec
from spawnd.state.repository import DeployedRepository
from spawnd.state.submission import consume_next_submission, enqueue_submission, submit_due_schedules, submit_plan, submit_template
from spawnd.workers.worker import drain_queue_outbox, reconcile_ready_agents


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    plan: PlanSpec | None = None
    run_id: str | None = None
    source_repo: str | None = None
    source_ref: str | None = None


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    name: str
    plan_template: str
    description: str | None = None
    source_repo_template: str | None = None
    source_ref_template: str | None = None


class TemplateRunBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    parameters: dict[str, Any] = {}
    run_id: str | None = None


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    template_id: str
    name: str
    interval_seconds: int
    parameters: dict[str, Any] = {}
    status: Literal['active', 'paused'] = 'active'


class ScheduleStatusBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    status: Literal['active', 'paused']


class SubmissionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kind: str
    plan: dict[str, Any] | None = None
    template_id: str | None = None
    parameters: dict[str, Any] = {}
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


def _verify_github_signature(secret: str, body: bytes, signature: str | None) -> None:
    if not signature:
        raise HTTPException(status_code=401, detail="Missing GitHub signature")
    prefix, separator, digest = signature.partition("=")
    if prefix != "sha256" or not separator:
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")


def _github_parameters(event: str, payload: dict[str, Any]) -> dict[str, str]:
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    parameters = {
        "event": event,
        "action": str(payload.get("action") or ""),
        "repo": str(repository.get("full_name") or repository.get("name") or ""),
        "clone_url": str(repository.get("clone_url") or ""),
        "ssh_url": str(repository.get("ssh_url") or ""),
        "default_branch": str(repository.get("default_branch") or ""),
        "ref": str(payload.get("ref") or ""),
        "before": str(payload.get("before") or ""),
        "after": str(payload.get("after") or ""),
    }
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head") if isinstance(pull_request.get("head"), dict) else {}
        base = pull_request.get("base") if isinstance(pull_request.get("base"), dict) else {}
        parameters.update(
            {
                "pr_number": str(pull_request.get("number") or payload.get("number") or ""),
                "head_ref": str(head.get("ref") or ""),
                "head_sha": str(head.get("sha") or ""),
                "base_ref": str(base.get("ref") or ""),
                "base_sha": str(base.get("sha") or ""),
            }
        )
    issue = payload.get("issue")
    if isinstance(issue, dict):
        parameters["issue_number"] = str(issue.get("number") or "")
    return parameters


def create_app() -> FastAPI:
    app = FastAPI(title="spawnd", version="0.1.0")

    @app.middleware("http")
    async def require_bearer_token(request: Request, call_next):
        if request.url.path in {"/healthz", "/readyz", "/metrics"}:
            return await call_next(request)
        if request.url.path.startswith("/webhooks/github/"):
            return await call_next(request)
        token = load_backend_config().api_token
        if not token:
            return JSONResponse({"detail": "SPAWND_API_TOKEN is required"}, status_code=500)
        authorization = request.headers.get("authorization") or ""
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value, token):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, bool]:
        config = load_backend_config()
        return {
            "database_configured": bool(config.database_url),
            "redis_configured": bool(config.redis_url),
            "api_auth_configured": bool(config.api_token),
        }

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        config = load_backend_config()
        try:
            queue_depth = _coordinator().queue_depth() if config.redis_url else -1
            submission_queue_depth = _coordinator().submission_queue_depth() if config.redis_url else -1
        except Exception:
            queue_depth = -1
            submission_queue_depth = -1
        try:
            workers = _repository().list_worker_nodes() if config.database_url else []
            worker_count = len(workers)
            stale_worker_count = len([worker for worker in workers if worker.get("stale")])
        except Exception:
            worker_count = -1
            stale_worker_count = -1
        lines = [
            "# HELP spawnd_backend_configured Backend configuration presence.",
            "# TYPE spawnd_backend_configured gauge",
            f"spawnd_backend_configured{{component=\"database\"}} {1 if config.database_url else 0}",
            f"spawnd_backend_configured{{component=\"redis\"}} {1 if config.redis_url else 0}",
            f"spawnd_backend_configured{{component=\"api_auth\"}} {1 if config.api_token else 0}",
            "# HELP spawnd_queue_depth Ready-agent queue depth, or -1 when unavailable.",
            "# TYPE spawnd_queue_depth gauge",
            f"spawnd_queue_depth {queue_depth}",
            "# HELP spawnd_submission_queue_depth Run-submission queue depth, or -1 when unavailable.",
            "# TYPE spawnd_submission_queue_depth gauge",
            f"spawnd_submission_queue_depth {submission_queue_depth}",
            "# HELP spawnd_worker_nodes Worker-node count, or -1 when unavailable.",
            "# TYPE spawnd_worker_nodes gauge",
            f"spawnd_worker_nodes {worker_count}",
            "# HELP spawnd_worker_nodes_stale Stale worker-node count, or -1 when unavailable.",
            "# TYPE spawnd_worker_nodes_stale gauge",
            f"spawnd_worker_nodes_stale {stale_worker_count}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")

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

    @app.post("/templates")
    def put_template(body: TemplateBody) -> dict[str, str]:
        _repository().create_run_template(
            body.id,
            name=body.name,
            description=body.description,
            plan_template=body.plan_template,
            source_repo_template=body.source_repo_template,
            source_ref_template=body.source_ref_template,
        )
        return {"template_id": body.id}

    @app.get("/templates")
    def list_templates(limit: int = 100) -> list[dict[str, Any]]:
        return _repository().list_run_templates(limit=limit)

    @app.post("/templates/{template_id}/runs")
    def run_template(template_id: str, body: TemplateRunBody) -> dict[str, str]:
        run_id = submit_template(
            template_id,
            parameters=body.parameters,
            repository=_repository(),
            coordinator=_coordinator(),
            run_id=body.run_id,
        )
        return {"run_id": run_id}

    @app.post("/schedules")
    def put_schedule(body: ScheduleBody) -> dict[str, str]:
        _repository().create_schedule(
            body.id,
            template_id=body.template_id,
            name=body.name,
            interval_seconds=body.interval_seconds,
            parameters=body.parameters,
            status=body.status,
        )
        return {"schedule_id": body.id}

    @app.patch("/schedules/{schedule_id}/status")
    def set_schedule_status(schedule_id: str, body: ScheduleStatusBody) -> dict[str, str]:
        try:
            _repository().set_schedule_status(schedule_id, status=body.status)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"schedule_id": schedule_id, "status": body.status}

    @app.post("/schedules/run-due")
    def run_due_schedules(limit: int = 100) -> list[dict[str, Any]]:
        return submit_due_schedules(repository=_repository(), coordinator=_coordinator(), limit=limit)

    @app.post("/submissions")
    def enqueue_submission_request(body: SubmissionBody) -> dict[str, str]:
        payload = body.model_dump(mode="json", exclude_none=True)
        enqueue_submission(_coordinator(), payload)
        return {"status": "queued"}

    @app.post("/submissions/drain")
    def drain_submission_queue(consumer_id: str = "api-submitter", block_ms: int = 0) -> dict[str, Any]:
        result = consume_next_submission(
            repository=_repository(),
            coordinator=_coordinator(),
            consumer_id=consumer_id,
            block_ms=block_ms,
        )
        return result or {"status": "empty"}

    @app.post("/webhooks/github/{template_id}")
    async def github_webhook(template_id: str, request: Request) -> dict[str, str]:
        secret = load_backend_config().github_webhook_secret
        if not secret:
            raise HTTPException(status_code=500, detail="SPAWND_GITHUB_WEBHOOK_SECRET is required")
        body = await request.body()
        _verify_github_signature(secret, body, request.headers.get("X-Hub-Signature-256"))
        event = request.headers.get("X-GitHub-Event") or "unknown"
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="GitHub payload must be a JSON object")
        if event == "ping":
            return {"status": "pong"}
        run_id = submit_template(
            template_id,
            parameters=_github_parameters(event, payload),
            repository=_repository(),
            coordinator=_coordinator(),
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

    @app.post("/workers/outbox/drain")
    def drain_outbox(limit: int = 100) -> list[dict[str, str]]:
        return drain_queue_outbox(_repository(), _coordinator(), limit=limit)

    @app.get("/workers")
    def workers() -> dict[str, Any]:
        coordinator = _coordinator()
        return {
            "queue_depth": coordinator.queue_depth(),
            "submission_queue_depth": coordinator.submission_queue_depth(),
            "workers": _repository().list_worker_nodes(),
        }

    return app
