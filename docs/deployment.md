# Deployment

Spawnd deployed mode has one durable state contract:

- Postgres stores runs, agents, attempts, events, runtime records, checks, usage,
  traces, artifacts metadata, provenance, workers, templates, schedules, and the
  queue outbox.
- Redis stores coordination hints only: ready jobs, leases, heartbeats, pubsub,
  and cancellation signals.
- S3-compatible object storage stores redacted artifact payloads.

## Required Environment

```bash
SPAWND_DATABASE_URL=postgresql+psycopg://user:pass@postgres:5432/spawnd
SPAWND_REDIS_URL=redis://redis:6379/0
SPAWND_API_TOKEN=change-me

SPAWND_ARTIFACTS_BUCKET=spawnd-artifacts
SPAWND_ARTIFACTS_ENDPOINT=https://s3.example.com
SPAWND_ARTIFACTS_REGION=us-east-1
SPAWND_ARTIFACTS_PREFIX=prod

AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Optional operational secrets:

```bash
SPAWND_GITHUB_WEBHOOK_SECRET=...
SPAWND_NOTIFICATION_WEBHOOK_URL=https://hooks.example.com/spawnd
SPAWND_NOTIFICATION_TIMEOUT_SECONDS=10
SPAWND_SOURCE_CACHE_ROOT=/var/lib/spawnd/sources
SPAWND_SCRATCH_ROOT=/var/lib/spawnd/scratch
```

Provider and git credentials should be supplied as worker environment variables
and referenced from plans with `agent.env_refs`. Do not put raw provider keys,
git tokens, or `.env` contents into plan YAML.

Write-capable provider runtimes must run inside an explicit worker isolation
boundary. Set `SPAWND_RUNTIME_ISOLATION=container`, `jail`, or `vm` only on
workers actually running in that boundary. Without it, workers fail mutable real
provider agents before setup/runtime execution.

## Migrations

Run migrations before starting API or worker processes:

```bash
alembic upgrade head
```

`spawnd worker --poll` and the HTTP API assume the schema already exists.

## Processes

API:

```bash
spawnd serve --host 0.0.0.0 --port 8765
```

Worker:

```bash
spawnd worker --poll --worker-id worker-a
```

Outbox drainer, if not relying on worker polling:

```bash
spawnd drain-outbox
```

Scheduler runner, for durable schedules:

```bash
spawnd schedules run-due
```

Submission queue consumer, for asynchronous external ingress:

```bash
spawnd submit-queue drain --poll --consumer-id submitter-a
```

Health and operations:

```bash
curl http://localhost:8765/healthz
curl http://localhost:8765/readyz
curl http://localhost:8765/metrics
spawnd workers
spawnd reconcile
```

All HTTP routes except health, readiness, metrics, and GitHub webhooks require:

```text
Authorization: Bearer $SPAWND_API_TOKEN
```

GitHub webhooks use `X-Hub-Signature-256` and
`SPAWND_GITHUB_WEBHOOK_SECRET`.

## Local Deployed Stack

Start Postgres, Redis, MinIO, API, and one worker:

```bash
docker compose up --build
```

The compose stack exposes:

- API: `http://localhost:8765`
- Postgres: `localhost:54329`
- Redis: `localhost:63799`
- MinIO: `http://localhost:9000`
- MinIO console: `http://localhost:9001`

The dev API token is `dev-token`.
