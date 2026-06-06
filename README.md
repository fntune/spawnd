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
spawnd run -f plan.yaml --source-repo "$PWD"
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
)
```

## Workers

Run a single claim:

```bash
spawnd worker --once --source-path /repo
```

Run a polling worker:

```bash
spawnd worker --poll --source-path /repo
```

Recover queue hints from Postgres:

```bash
spawnd reconcile
```

Record a heartbeat:

```bash
spawnd worker-heartbeat
```

Workers claim through a Postgres transaction, renew lease state while running,
write artifacts and provenance, and enqueue newly ready dependents through the
queue outbox plus Redis wakeups.

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
```

## HTTP API

Serve the API:

```bash
spawnd serve --host 0.0.0.0 --port 8765
```

Endpoints:

- `POST /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/checks`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/traces`
- `GET /runs/{run_id}/provenance`
- `POST /runs/{run_id}/cancel`
- `POST /runs/{run_id}/resume`
- `POST /workers/reconcile`

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
