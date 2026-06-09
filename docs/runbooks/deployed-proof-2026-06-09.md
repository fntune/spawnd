# Deployed Proof Run - 2026-06-09

This runbook records the deployed Spawnd proof performed against the local Docker
stack on 2026-06-09. The stack used real Postgres, Redis, and MinIO. Runtime
execution was mock-backed, so the full unattended goal is not complete yet.

## Environment

Compose services:

- Postgres: `localhost:54329`, database `spawnd`
- Redis: `localhost:63799`
- MinIO: `localhost:9000`, bucket `spawnd-artifacts`
- API: `localhost:8765`, token `dev-token`
- Worker: `spawnd worker --poll --worker-id compose-worker-1`
- Submitter: `spawnd submit-queue drain --poll --consumer-id compose-submitter-1`
- Scheduler: `spawnd schedules run-due --poll --idle-sleep-seconds 60`
- Outbox drainer: `spawnd drain-outbox --poll --idle-sleep-seconds 5`

Configured service env shape:

- `SPAWND_DATABASE_URL=postgresql+psycopg://spawnd:spawnd@postgres:5432/spawnd`
- `SPAWND_REDIS_URL=redis://redis:6379/0`
- `SPAWND_API_TOKEN=dev-token`
- `SPAWND_GITHUB_WEBHOOK_SECRET=dev-webhook-secret`
- `SPAWND_ARTIFACTS_BUCKET=spawnd-artifacts`
- `SPAWND_ARTIFACTS_ENDPOINT=http://minio:9000`
- `SPAWND_ARTIFACTS_REGION=us-east-1`
- `SPAWND_ARTIFACTS_PREFIX=dev`
- `SPAWND_SCRATCH_ROOT=/scratch`
- `SPAWND_RUNTIME_ISOLATION=container`
- `AWS_ACCESS_KEY_ID=spawnd`
- `AWS_SECRET_ACCESS_KEY=spawnd-secret`

Worker git identity for local proof commits:

- `GIT_AUTHOR_NAME=spawnd`
- `GIT_AUTHOR_EMAIL=spawnd@example.invalid`
- `GIT_COMMITTER_NAME=spawnd`
- `GIT_COMMITTER_EMAIL=spawnd@example.invalid`

Provider and GitHub credentials were not present:

- `OPENAI_API_KEY` unset
- `ANTHROPIC_API_KEY` unset
- `CLAUDE_API_KEY` unset
- `GITHUB_TOKEN` unset
- `GH_TOKEN` unset

## Infrastructure Commands

Build and start:

```bash
docker compose build api worker submitter scheduler outbox
docker compose up -d api worker submitter scheduler outbox
```

Migration proof:

```bash
docker compose exec -T postgres psql -U spawnd -d spawnd -c "select version_num from alembic_version;"
```

Result:

```text
0002_unattended_readiness
```

Health proof:

```bash
curl -fsS -H 'Authorization: Bearer dev-token' http://localhost:8765/healthz
curl -fsS -H 'Authorization: Bearer dev-token' http://localhost:8765/readyz
curl -fsS -H 'Authorization: Bearer dev-token' http://localhost:8765/metrics
```

Results:

- `/healthz`: `{"status":"ok"}`
- `/readyz`: database, Redis, and API auth all configured
- `/metrics`: ready-agent queue depth `0`, submission queue depth `0`

Service proof after adding scheduler/outbox services:

```bash
docker compose ps api worker submitter scheduler outbox redis postgres minio
```

All listed services were `Up`; Postgres, Redis, and MinIO were healthy.

## Bugs Found And Fixed

Two deployed-stack failures were found during proof runs:

1. `infra-proof-20260609093643` failed because `_git_repo_lock` used
   `yield from _file_lock(...)` against a context manager.
2. `infra-proof-20260609093947` failed because worker git identity was missing,
   so `git commit` could not create the proof commit.

Fixes:

- `_git_repo_lock` now enters `_file_lock(...)` with a `with` block.
- Worker compose env sets author and committer identity for local proof commits.
- Redis stream reads now recreate missing stream groups after Redis state loss.
- Redis acks tolerate missing groups because Redis is a coordination hint plane.
- CLI now supports polling scheduler and outbox-drainer modes.
- Compose now runs `scheduler` and `outbox` services.
- `spawnd pr create` now runs `gh pr create` from the provenance repo path and
  writes the created PR URL/number back to `git_provenance`.

## Completed Deployed Proof Run

Run id:

```text
infra-proof-20260609094105
```

Submission source:

- `source_repo=https://github.com/fntune/spawnd.git`
- `source_ref=origin/main`
- submitted through HTTP API
- worker execution command:
  `docker compose run --rm worker spawnd worker --once --mock --worker-id proof-worker-3 --block-ms 10000`

Plan shape:

- setup command: `python -c "open('spawnd-proof.txt','w').write('proof')"`
- check command: `python -c "assert open('spawnd-proof.txt').read() == 'proof'"`
- git commit enabled
- git push disabled
- artifact raw capture disabled
- telemetry exporter disabled; local trace mirror still recorded spans

Worker result:

```text
Worker proof-worker-3 finished infra-proof-20260609094105/proof: completed
```

Postgres counts:

```text
run_id                       agents attempts events checks traces artifacts provenance
infra-proof-20260609094105        1        1      6      1     11         5          1
```

Git provenance:

- base ref: `origin/main`
- base/head source commit: `0307d5f6f72a24f1eb89f7466f776794a61999be`
- proof commit: `a5919e73283220314a9a4a8e5d25c58540d396f2`
- branch: `spawnd/infra-proof-20260609094105/proof`
- changed files: `1`
- insertions: `1`
- deletions: `0`

PR proof:

```bash
docker compose run --rm -e GH_TOKEN="$(gh auth token)" -e DEBIAN_FRONTEND=noninteractive worker sh -lc \
  'set -e; command -v gh || (apt-get update >/dev/null && apt-get install -y gh >/dev/null); git config --global credential.helper "!f() { echo username=x-access-token; echo password=\$GH_TOKEN; }; f"; spawnd pr create infra-proof-20260609094105 --agent proof --title-prefix spawnd-proof'
```

Result:

```text
https://github.com/fntune/spawnd/pull/7
```

Verification:

```text
remote branch: refs/heads/spawnd/infra-proof-20260609094105/proof
remote sha: a5919e73283220314a9a4a8e5d25c58540d396f2
pr_url: https://github.com/fntune/spawnd/pull/7
pr_number: 7
headRefName: spawnd/infra-proof-20260609094105/proof
baseRefName: main
state: OPEN
```

MinIO objects:

```text
dev/runs/infra-proof-20260609094105/proof/check-output-3f12fa262347478f85095101307d086f.txt
dev/runs/infra-proof-20260609094105/proof/final-message-0fc6a00a168f4b719555a7672fb4a460.txt
dev/runs/infra-proof-20260609094105/proof/patch-0ce1297cad6a4d7dbdcdff2ce631eb50.txt
dev/runs/infra-proof-20260609094105/proof/runtime-output-76617fcfb7be4020bc52594111a5c300.txt
dev/runs/infra-proof-20260609094105/proof/setup-output-976c561ab4a4472d9367fdfaf210b85a.txt
```

Artifact contents checked:

- final message: `Fake runtime completed proof`
- patch artifact adds `spawnd-proof.txt` containing `proof`

## Redis Loss Recovery

Status reconstruction after Redis flush:

```bash
docker compose exec -T redis redis-cli flushdb
curl -fsS -H 'Authorization: Bearer dev-token' http://localhost:8765/runs/infra-proof-20260609094105
```

Result:

```text
infra-proof-20260609094105 completed
proof completed spawnd/infra-proof-20260609094105/proof
attempts 1
trace_span_count 11
```

Process survival after Redis flush:

```bash
docker compose exec -T redis redis-cli flushdb
docker compose ps worker submitter scheduler outbox redis
```

Result:

- worker stayed `Up`
- submitter stayed `Up`
- scheduler stayed `Up`
- outbox drainer stayed `Up`
- Redis stayed healthy

This proves Redis can lose coordination keys without taking down the deployed
control-plane processes. Postgres remains the reconstructable state store.

## Retry Proof

Run id:

```text
retry-proof-20260609094348
```

Procedure:

- Submitted a proof run.
- Claimed the agent as worker `lost-worker`.
- Aged the lease to stale.
- Ran `docker compose run --rm worker spawnd reconcile`.
- Ran `docker compose run --rm worker spawnd worker --once --mock --worker-id retry-worker-1 --block-ms 10000`.

Result:

```text
retry-proof-20260609094348 completed
proof completed retry_count=1
attempt 1 expired lost-worker
attempt 2 completed retry-worker-1
```

Postgres counts:

```text
run_id                       agents attempts events checks traces artifacts provenance
retry-proof-20260609094348        1        2      8      1     11         5          1
```

## Cancellation Proof

Run id:

```text
cancel-proof-20260609094448
```

Procedure:

- Submitted a proof run.
- Called `POST /runs/cancel-proof-20260609094448/cancel`.
- Ran a one-shot mock worker.

Result:

```text
{"cancelled":1}
Worker cancel-worker-1 found no ready agents
cancel-proof-20260609094448 cancelled
proof cancelled
attempts 0
```

Events:

- `run_created`
- `run_cancelled`

## Templates, Schedules, And Webhook Mapping

Contributor templates created:

```text
contributor-subport   https://github.com/fntune/subport.git   origin/main
contributor-stockbay  https://github.com/fntune/stockbay.git  origin/main
contributor-fn        https://github.com/fntune/fn.git        origin/main
contributor-biomon    https://github.com/fntune/biomon.git    origin/main
contributor-cashgrep  https://github.com/fntune/cashgrep.git  origin/main
github-contributor    {clone_url}                             {after}
```

Paused nightly schedules created:

```text
contributor-subport-nightly
contributor-stockbay-nightly
contributor-fn-nightly
contributor-biomon-nightly
contributor-cashgrep-nightly
```

All schedules are `paused`, interval `86400`, pending credential wiring.

Webhook receiver proof:

- `SPAWND_GITHUB_WEBHOOK_SECRET=dev-webhook-secret`
- signed GitHub-style webhook created `github-contributor-4abd61b3`
- run was cancelled through the API:
  `POST /runs/github-contributor-4abd61b3/cancel`

Result:

```text
{"cancelled":1}
github-contributor-4abd61b3 cancelled
contributor cancelled
```

Actual GitHub webhook installation on the repositories was not performed because
no GitHub credential was available.

## Verification

Focused tests:

```bash
pytest -q tests/test_redis_coordination.py tests/test_deployed_worker.py tests/test_git.py
```

Result:

```text
17 passed, 14 skipped
```

Compile checks:

```bash
python -m compileall -q spawnd/cli.py spawnd/coordination/redis.py spawnd/workers/worker.py tests/test_redis_coordination.py
```

Result: passed.

## Remaining Work Before Goal Completion

The active unattended goal is not complete. Remaining required proof:

- Wire real provider credentials into worker runtime env or secret refs.
- Run a real provider-backed contributor job, not `--mock`.
- Persist GitHub credentials or secret refs for unattended branch push and PR
  creation; the proof used an explicit one-off `GH_TOKEN`.
- Install real GitHub webhooks on the intended repositories.
- Activate schedules only after provider and GitHub credentials are available.
- Optional OTLP collector/export was not enabled; only the Postgres trace mirror
  was proven.
- Validate the intended real runtime path in the worker image. The OpenAI Python
  package, OpenAI Codex Python package, and `claude-agent-sdk` are present;
  Anthropic Python package is not present, and no `codex` binary is currently on
  the worker `PATH`.
