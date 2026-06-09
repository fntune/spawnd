# spawnd.dev

Spawnd is a deployed agent orchestration system. Postgres is the durable system
of record, Redis is coordination, and object storage holds redacted artifacts.
Workers use git worktrees and files only as execution scratch; durable evidence
is stored as database rows plus artifact objects.

## Architecture

- **Postgres** stores runs, agents, attempts, leases, events, responses,
  runtime sessions, invocations, provider events, traces, artifacts, checks,
  usage, and git provenance.
- **Redis** carries ready-agent wakeups, worker heartbeats, lease hints, live
  run events, and cancellation signals. Redis can be rebuilt from Postgres.
- **Object storage** stores redacted setup output, runtime output, final
  messages, check output, patches, and provider payloads.
- **OpenTelemetry** can export spans externally while Postgres keeps a redacted
  queryable trace mirror.
- **CLI, Python API, and HTTP API** submit to and read from the same deployed
  services.

## Configuration

Set the deployed backend environment:

```bash
export SPAWND_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/spawnd'
export SPAWND_REDIS_URL='redis://host:6379/0'
export SPAWND_ARTIFACTS_BUCKET='spawnd-artifacts'
export SPAWND_ARTIFACTS_ENDPOINT='https://s3.example.com'
export SPAWND_ARTIFACTS_REGION='us-east-1'
export SPAWND_ARTIFACTS_PREFIX='prod'
export SPAWND_API_TOKEN='change-me'
```

Telemetry uses standard `OTEL_*` variables plus plan or env settings:

```yaml
orchestration:
  telemetry:
    enabled: true
    exporter: otlp
    capture: full
    failure_policy: degrade
  artifacts:
    capture_raw: false
```

Raw artifact capture is off by default. Runtime output, final messages, setup
output, checks, and provider payloads are redacted before upload unless the
plan explicitly enables raw capture.

## Database

Use Alembic for deployed schema creation:

```bash
alembic upgrade head
```

The greenfield initial migration creates the complete deployed schema:
materialized run and agent state, normalized runtime/provider tables, artifact
indexes, verification checks, trace mirrors, git provenance, worker nodes, and
queue outbox.

## Submit Runs

Submit a plan file:

```bash
spawnd run -f plan.yaml --source-repo "$PWD" --source-ref origin/main
```

`spawnd submit -f plan.yaml` is an alias for the same deployed submit path.

Inline runs are supported:

```bash
spawnd run -p "Improve the parser error message" --check "pytest tests/test_parser.py"
```

Python clients use the same backend:

```python
from spawnd import agent, run

run(
    [agent("parser", "Improve parser diagnostics", check="pytest tests/test_parser.py")],
    repository=repo,
    coordinator=coordinator,
    source_repo="/repo",
    source_ref="origin/main",
)
```

`source_repo` is a local git repository path that workers can read. The CLI
defaults it to the submitter's current directory when omitted. `source_ref` is
the default base ref for worker-created worktrees; a plan-level
`orchestration.worktree_source.base_ref` intentionally overrides it.

```yaml
orchestration:
  worktree_source:
    base_ref: origin/main
    fetch: true
    env_refs:
      GIT_ASKPASS: SPAWND_GIT_ASKPASS
  worktree_setup:
    command: bash scripts/worktree/setup.sh
    cache: true
    cache_paths:
      - pnpm-lock.yaml
  command_policy:
    mode: allowlist
  cleanup:
    worktree: true
  concurrency_limit: 2
```

`source_repo` may be a local git repository path or a git remote URL. Remote
sources are cloned/fetched into the worker source cache under
`SPAWND_SOURCE_CACHE_ROOT` or `$SPAWND_SCRATCH_ROOT/sources`. Worker worktrees
remain scratch; durable evidence is still Postgres rows plus object-store
artifacts.

Git clone/fetch/push credentials should be provided through explicit
`env_refs` on `orchestration.worktree_source` or `orchestration.git`. The plan
stores only the reference names; workers resolve actual secret values from their
environment at runtime.
The compose worker exposes `SPAWND_GITHUB_TOKEN` and
`SPAWND_GIT_ASKPASS=/usr/local/bin/spawnd-git-askpass` for this purpose, and
can mount Codex auth from `SPAWND_CODEX_AUTH_DIR` into `/root/.codex`.

Plan-provided setup and check commands are constrained by
`orchestration.command_policy`. The default allowlist permits common test and
package-manager commands and rejects shell control syntax such as pipes,
semicolons, redirects, and command substitution. Use `mode: unrestricted` only
for trusted internal templates.

When `worktree_setup.cache` is true, workers compute a secret-free cache key
from the setup command, source HEAD, and configured lockfiles, create a cache
directory, and pass `SPAWND_SETUP_CACHE_KEY` plus `SPAWND_SETUP_CACHE_DIR` to
the setup command. Workers also point common package-manager caches inside that
directory with `npm_config_cache`, `npm_config_store_dir`, `YARN_CACHE_FOLDER`,
`BUN_INSTALL_CACHE_DIR`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, and
`POETRY_CACHE_DIR`.

`orchestration.cleanup.worktree: true` removes worker scratch worktrees after
terminal agent execution. Provenance, patches, checks, logs, and pushed branch
state remain durable in Postgres, object storage, and git.

`orchestration.concurrency_limit` is enforced at the Postgres claim boundary
for a run, so multiple worker processes cannot exceed the configured number of
simultaneously running agents.

Reviewer/read-only agents can be made non-mutating:

```yaml
agents:
  - name: reviewer
    use_role: reviewer
    write_allowed: false
    prompt: Review the current diff and report risks.
```

The built-in `reviewer` role defaults to `write_allowed: false`; explicit agent
configuration can override it.

Codex agents can set runtime safety policy per agent instead of relying on
process-wide env defaults:

```yaml
agents:
  - name: codex-worker
    runtime: codex
    codex:
      engine: cli
      sandbox: workspace-write
      approval_mode: deny_all
      ephemeral: true
    prompt: Make the requested change.
```

Codex SDK runs estimate cost from exposed token counts and enforce
`max_cost_usd`. Codex CLI runs with `--json`, records subprocess facts, extracts
session/token facts when the CLI emits them, and estimates cost from those token
counts. When a stored Codex CLI session id exists, retries use
`codex exec resume <session-id>`.

Claude, OpenAI, and Codex CLI agents can receive external MCP servers from the
plan. Secret material must be passed by reference and resolved from the worker
environment:

```yaml
defaults:
  runtime: claude
  mcp_servers:
    - name: docs
      type: http
      url: https://mcp.example.com
      header_refs:
        Authorization: SPAWND_MCP_DOCS_AUTHORIZATION
      tools:
        - search
```

Configured MCP servers are recorded in `runtime_mcp_servers` with a config hash.
Codex supports stdio MCP servers and streamable HTTP servers with bearer-token
environment references. Codex rejects unsupported MCP shapes, such as SSE,
literal HTTP headers, and non-Authorization header refs, at plan validation.
Codex manager agents use the CLI engine so they can access Spawnd coordination
tools through the internal `spawnd` MCP server.

## Workers

Run a single claim:

```bash
spawnd worker --once
```

Run a polling worker:

```bash
spawnd worker --poll
```

Write-capable real provider runtimes require an explicit worker isolation
boundary. Set `SPAWND_RUNTIME_ISOLATION=container`, `jail`, or `vm` on deployed
workers after placing them in that boundary. Mock runs and readonly agents do
not require it. Without this marker, the worker fails the agent before setup or
provider execution instead of running write/edit tools on an unisolated host.

Recover queue hints from Postgres:

```bash
spawnd reconcile
```

Record a heartbeat:

```bash
spawnd worker-heartbeat
```

Workers can serve runs for any submitted local `source_repo` they can read.
`--source-path /repo` is optional and is only the fallback source when a run has
no `source_repo`.

Workers claim through a Postgres transaction, move the run to `running`, renew
lease state while executing, write artifacts and provenance, and enqueue newly
ready dependents through the queue outbox plus Redis wakeups. Redis entries are
wakeups only: reconciliation rebuilds missing hints from Postgres and records
outbox rows before publishing.

When a provider exposes a resumable session id, workers persist it on the
runtime session. Claude retries pass the latest prior session id back through
the SDK `resume` option. OpenAI retries reuse the latest server-managed
conversation id through the Agents SDK `conversation_id` option. Codex CLI
retries resume with `codex exec resume` when a stored session id exists; Codex
SDK retries use `thread_resume` or `resume_thread` when the installed SDK exposes
one, and fail clearly rather than silently cold-starting if it does not.

Manager agents can spawn dynamic workers. `spawn_worker` creates a real queued
agent row, updates the durable run spec used by workers, records a queue outbox
row, and publishes a Redis wakeup when Redis is available. Spawned agents are
still claimed and executed through the same Postgres worker path. Claude and
OpenAI managers receive coordination tools through their SDK tool interfaces;
Codex managers receive the same tools through the internal Spawnd MCP server.

If the submitted source path is missing or is not a git repository, the worker
fails the claimed agent with a redacted source-error artifact and a
`runtime_errors(source='worktree_source')` row instead of leaving the agent
running.

## Inspect Runs

```bash
spawnd status <run-id>
spawnd events <run-id>
spawnd live-events <run-id>
spawnd artifacts <run-id>
spawnd logs <run-id>
spawnd checks <run-id>
spawnd trace <run-id>
spawnd provenance <run-id>
```

`spawnd logs` reads redacted runtime output and final messages from artifact
storage; it does not read worker filesystem state.

Control commands:

```bash
spawnd cancel <run-id>
spawnd resume <run-id>
spawnd pr create <run-id> --agent parser
spawnd pr merge <run-id> --agent parser --method squash
```

## HTTP API

Serve the API:

```bash
spawnd serve --host 0.0.0.0 --port 8765
```

Endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /metrics`
- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/checks`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/traces`
- `GET /runs/{run_id}/provenance`
- `POST /runs/{run_id}/cancel`
- `POST /runs/{run_id}/resume`
- `POST /templates`
- `GET /templates`
- `POST /templates/{template_id}/runs`
- `POST /schedules`
- `POST /schedules/run-due`
- `POST /submissions`
- `POST /submissions/drain`
- `POST /webhooks/github/{template_id}`
- `POST /workers/reconcile`
- `POST /workers/outbox/drain`
- `GET /workers`

`POST /runs` accepts a serialized plan body. It does not read server-local plan
files.

```json
{
  "run_id": "optional-run-id",
  "plan": {
    "name": "example",
    "agents": [
      {"name": "parser", "prompt": "Improve parser diagnostics"}
    ]
  },
  "source_repo": "/repo",
  "source_ref": "origin/main"
}
```

The server validates the plan at the HTTP boundary and rejects unknown request
fields.

All HTTP routes except health, readiness, metrics, and GitHub webhooks require
`Authorization: Bearer $SPAWND_API_TOKEN`. GitHub webhooks verify
`X-Hub-Signature-256` against `SPAWND_GITHUB_WEBHOOK_SECRET`.

Reusable templates and schedules are durable Postgres records:

```bash
spawnd templates put contributor -f contributor-template.yaml \
  --source-repo-template '{clone_url}' \
  --source-ref-template '{after}'
spawnd templates run contributor --param clone_url=https://github.com/acme/app.git --param after=main
spawnd schedules put nightly --template-id contributor --interval-seconds 86400
spawnd schedules run-due --poll --idle-sleep-seconds 60
spawnd submit-queue enqueue-template contributor --param clone_url=https://github.com/acme/app.git
spawnd submit-queue drain --once
```

External systems can enqueue JSON messages onto the Redis submission stream
through `POST /submissions` or directly to Redis. `spawnd submit-queue drain
--poll` consumes those messages, validates them, and creates canonical Postgres
runs.

Run `spawnd drain-outbox --poll --idle-sleep-seconds 5` as a separate service
when you want an independent outbox relay in addition to the worker's poll-loop
drain.

Notifications use backend environment configuration, not raw secrets in plans.
Set `SPAWND_NOTIFICATION_WEBHOOK_URL`; failed, timed-out, cancelled, and
cost-exceeded runs notify automatically. Completed runs notify when the plan has
`on_complete: notify`.

See [docs/deployment.md](docs/deployment.md) for container, compose, migration,
and production environment details.

## Development

Install with deployed extras:

```bash
pip install -e '.[dev,deployed,telemetry,codex,sdk,openai]'
```

State-transition integration tests require a Postgres test database:

```bash
export SPAWND_TEST_DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/spawnd_test'
pytest
```

Without `SPAWND_TEST_DATABASE_URL`, Postgres integration tests are skipped and
unit tests still run.

Useful local verification commands:

```bash
python -m compileall -q spawnd tests
git diff --check
```
