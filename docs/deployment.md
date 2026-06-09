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

The container worker exposes the stable secret-reference names used by deployed
plans:

- `SPAWND_CODEX_AUTH_DIR` mounts Codex auth into `/root/.codex`.
- `SPAWND_GITHUB_TOKEN` is loaded from the ignored deployment `.env` and is
  available to git through the askpass helper.
- `SPAWND_GIT_ASKPASS=/usr/local/bin/spawnd-git-askpass` points git HTTPS
  auth at the committed non-secret askpass helper.

For local compose runs, copy `.env.example` to `.env`, set
`SPAWND_CODEX_AUTH_DIR` and `SPAWND_GITHUB_TOKEN`, and keep `.env` untracked.
The helper prints only the token value already present in the worker
environment; it never stores credentials.

The Podman scripts and compose file use fully qualified registry image names
and avoid nested environment-default expansion. Podman hosts should use the
explicit scripts under `deploy/podman/` because `podman-compose` does not
reliably preserve the one-shot migration and init ordering that this stack
requires.

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

Outbox drainer:

```bash
spawnd drain-outbox --poll --idle-sleep-seconds 5
```

Scheduler runner, for durable schedules:

```bash
spawnd schedules run-due --poll --idle-sleep-seconds 60
```

Create schedules with an explicit status, and flip existing schedules without
rewriting their `next_run_at`:

```bash
spawnd schedules put nightly --template-id contributor --interval-seconds 86400 --status paused
spawnd schedules set-status nightly --status active
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

The local compose stack also starts an OpenTelemetry collector for worker trace
export:

- OTLP gRPC: `localhost:4317`
- OTLP HTTP: `localhost:4318`
- collector health: `http://localhost:13133`

Workers use `SPAWND_TELEMETRY_ENABLED=1`,
`SPAWND_TELEMETRY_EXPORTER=otlp`, and
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` with
`SPAWND_TELEMETRY_FAILURE_POLICY=degrade`.

All HTTP routes except health, readiness, metrics, and GitHub webhooks require:

```text
Authorization: Bearer $SPAWND_API_TOKEN
```

GitHub webhooks use `X-Hub-Signature-256` and
`SPAWND_GITHUB_WEBHOOK_SECRET`. GitHub `ping` events return
`{"status":"pong"}` and do not submit runs, so hook creation can be verified
without starting contributor work.

After the API has a durable public HTTPS URL, install repository webhooks:

```bash
export SPAWND_GITHUB_WEBHOOK_SECRET='same-secret-as-api'
spawnd github-webhooks install \
  --base-url https://spawnd.example.com \
  --repo fntune/subport \
  --repo fntune/stockbay \
  --repo fntune/fn \
  --repo fntune/biomon \
  --repo fntune/cashgrep
```

Use `--dry-run` first to validate the URL and show whether each repo would be
created or updated. The installer rejects localhost and non-HTTPS callback
targets.

Verify installed hooks with the same target:

```bash
spawnd github-webhooks verify \
  --base-url https://spawnd.example.com \
  --repo fntune/subport \
  --repo fntune/stockbay \
  --repo fntune/fn \
  --repo fntune/biomon \
  --repo fntune/cashgrep
```

Install the reusable contributor template and paused nightly schedules:

```bash
spawnd templates put github-contributor -f deploy/templates/contributor.yaml \
  --source-repo-template '{clone_url}' \
  --source-ref-template '{after}'

spawnd templates put contributor-subport -f deploy/templates/contributor.yaml \
  --source-repo-template https://github.com/fntune/subport.git \
  --source-ref-template origin/main
spawnd schedules put contributor-subport-nightly \
  --template-id contributor-subport \
  --interval-seconds 86400 \
  --status paused \
  --param event=schedule \
  --param action=nightly \
  --param repo=fntune/subport \
  --param repo_slug=fntune-subport \
  --param ref=origin/main \
  --param before= \
  --param after=origin/main \
  --param pr_number= \
  --param head_ref= \
  --param head_sha= \
  --param base_ref= \
  --param base_sha=
```

Repeat the project-specific template/schedule pair for each repository that
should have recurring runs. Keep schedules paused until the worker credentials,
webhook delivery, and cost budget are verified in the target environment.

## Podman Deployed Stack

Start Postgres, Redis, MinIO, API, and one worker:

```bash
cp .env.example .env
$EDITOR .env
deploy/podman/up.sh
```

The Podman stack exposes:

- API: `http://localhost:8765`
- Postgres: `localhost:54329`
- Redis: `localhost:63799`
- MinIO: `http://localhost:9000`
- MinIO console: `http://localhost:9001`
- OTLP collector HTTP: `http://localhost:4318`
- OTLP collector health: `http://localhost:13133`

The dev API token is `dev-token`.

The worker reads persistent credentials from `.env`. `SPAWND_CODEX_AUTH_DIR`
must point at a readable Codex auth directory and `SPAWND_GITHUB_TOKEN` is used
only by the worker git askpass helper.

```bash
deploy/podman/down.sh
deploy/podman/down.sh --volumes  # removes Postgres, MinIO, and scratch state
```

The Compose file remains available for environments that intentionally run
Compose, but `micro-1` and other Podman hosts should use the Podman scripts.

For Tailscale Funnel ingress on a Podman host:

```bash
tailscale funnel --bg 8765
curl -fsS https://micro-1.example.ts.net/readyz
```

Then use that HTTPS base URL with `spawnd github-webhooks install`.
