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

Outbox drainer:

```bash
spawnd drain-outbox --poll --idle-sleep-seconds 5
```

Scheduler runner, for durable schedules:

```bash
spawnd schedules run-due --poll --idle-sleep-seconds 60
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

## Real Codex Contributor Job

The mock proof in `docs/runbooks/deployed-proof-2026-06-09.md` validates the
deployed control plane. To run the same path with a real Codex-backed
contributor, use a worker image that has the `codex` CLI available, mount the
Codex auth directory into the worker container read-only, and provide a GitHub
token only through environment variables.

Minimum operator prerequisites:

- `codex exec --json` works inside the worker image.
- `gh` is available in the image or installed before running `spawnd pr create`.
- The mounted Codex auth directory is readable by the worker user. The compose
  image runs as root, so the default mount target is `/root/.codex`.
- The GitHub token has `contents:write` for branch pushes and
  `pull_requests:write` for PR creation.
- The worker still has `SPAWND_RUNTIME_ISOLATION=container`, `jail`, or `vm`.

Use an askpass helper for git HTTPS authentication. Store the helper in your
operator-managed secret/config path, not in the repository:

```sh
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *) printf '%s\n' "$GITHUB_TOKEN" ;;
esac
```

Submit a plan that references worker environment names instead of embedding
secrets:

```yaml
name: real-codex-contributor
orchestration:
  worktree_source:
    base_ref: origin/main
    fetch: true
    env_refs:
      GIT_ASKPASS: SPAWND_GIT_ASKPASS
      GITHUB_TOKEN: SPAWND_GITHUB_TOKEN
  git:
    commit: true
    push: true
    remote: origin
    env_refs:
      GIT_ASKPASS: SPAWND_GIT_ASKPASS
      GITHUB_TOKEN: SPAWND_GITHUB_TOKEN
agents:
  - name: contributor
    runtime: codex
    codex:
      engine: cli
      sandbox: workspace-write
      approval_mode: deny_all
      ephemeral: true
    prompt: Make one small, safe improvement and keep the change narrow.
    check: pytest -q
```

Submit against the real repository URL:

```bash
export SPAWND_API_TOKEN=dev-token
spawnd run -f real-codex-contributor.yaml \
  --source-repo https://github.com/OWNER/REPO.git \
  --source-ref origin/main
```

Run a mounted one-shot worker for proof, or add the same mounts/env to the
polling worker service:

```bash
export GITHUB_TOKEN=...
docker compose run --rm \
  -v "$HOME/.codex:/root/.codex:ro" \
  -v "/secure/path/spawnd-git-askpass:/run/spawnd/git-askpass:ro" \
  -e SPAWND_GITHUB_TOKEN="$GITHUB_TOKEN" \
  -e SPAWND_GIT_ASKPASS=/run/spawnd/git-askpass \
  worker spawnd worker --once --worker-id real-codex-proof-1
```

Open the PR from recorded provenance with the same GitHub token available to
`gh` and git:

```bash
docker compose run --rm \
  -v "/secure/path/spawnd-git-askpass:/run/spawnd/git-askpass:ro" \
  -e GH_TOKEN="$GITHUB_TOKEN" \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" \
  -e GIT_ASKPASS=/run/spawnd/git-askpass \
  worker spawnd pr create <run-id> --agent contributor --title-prefix spawnd
```

Do not pass `--mock` for this proof. Verify the run with `spawnd status`,
`spawnd checks`, `spawnd provenance`, and the returned GitHub PR URL.
