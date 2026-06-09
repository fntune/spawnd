# Deployed Proof Run - 2026-06-09

This runbook records the deployed Spawnd proof performed against the local Docker
stack on 2026-06-09. The stack used real Postgres, Redis, and MinIO. It includes
both an initial mock runtime proof and a later real Codex-backed contributor run.
The full unattended goal is still not complete because real GitHub webhook
installation requires a durable public API callback URL.

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

Before the follow-up persistent wiring, provider and GitHub environment
variables were not present in the compose stack:

- `OPENAI_API_KEY` unset
- `ANTHROPIC_API_KEY` unset
- `CLAUDE_API_KEY` unset
- `GITHUB_TOKEN` unset
- `GH_TOKEN` unset

The real Codex contributor proof used a one-shot worker with host Codex auth
mounted into `/root/.codex` and a one-off `GH_TOKEN` injected from `gh auth
token`. That proves the runtime and PR path, but it is not persistent unattended
secret wiring.

Follow-up persistent wiring added after the proof:

- `docker/git-askpass.sh` is a committed non-secret helper for git HTTPS auth.
- the worker image includes `gh` plus `/usr/local/bin/spawnd-git-askpass`.
- compose worker env exposes `SPAWND_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`,
  `SPAWND_GIT_ASKPASS`, and `GIT_ASKPASS`.
- compose worker mounts `${SPAWND_CODEX_AUTH_DIR:-${HOME}/.codex}` into
  `/root/.codex:ro`.
- `.env.example` documents the local secret names while `.env` remains ignored.

Persistent wiring verification after recreating the worker from `.env`:

```bash
umask 077
# write SPAWND_CODEX_AUTH_DIR and SPAWND_GITHUB_TOKEN into ignored .env
docker compose up -d --force-recreate worker
docker compose exec -T worker sh -lc '
test -r /root/.codex/auth.json
command -v gh
gh --version | head -1
command -v spawnd-git-askpass
test "$SPAWND_GIT_ASKPASS" = /usr/local/bin/spawnd-git-askpass
test "$GIT_ASKPASS" = /usr/local/bin/spawnd-git-askpass
test -n "$SPAWND_GITHUB_TOKEN"
test -n "$GH_TOKEN"
printf "codex_auth=readable\n"
printf "github_token=set\n"
printf "askpass=%s\n" "$SPAWND_GIT_ASKPASS"
'
docker compose exec -T worker sh -lc 'gh api user --jq .login'
```

Result:

```text
/usr/bin/gh
gh version 2.46.0 (2025-01-13 Debian 2.46.0-3)
/usr/local/bin/spawnd-git-askpass
codex_auth=readable
github_token=set
askpass=/usr/local/bin/spawnd-git-askpass
sour4bh
```

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

## Real Codex Contributor Proof

Two failed attempts preceded the successful contributor run:

- `real-contributor-20260609044107` reached Codex but failed because
  `gpt-5.1-codex-mini` is not supported for this ChatGPT-authenticated Codex
  account.
- `real-contributor-20260609044155` completed with real token/cost usage but
  made no repository change because Codex SDK `workspace-write` sandboxing failed
  inside the container with a namespace/bwrap error.

The successful run used container isolation plus Codex SDK `danger-full-access`
inside the container. This was bounded by the worker container, runtime timeout,
check timeout, cost cap, and explicit command policy for setup/check commands.

Run id:

```text
real-contributor-20260609044421
```

Submission source:

- `source_repo=https://github.com/fntune/spawnd.git`
- `source_ref=origin/main`
- submitted through HTTP API
- runtime: Codex SDK with mounted host Codex auth
- worker id: `real-codex-worker-3`
- worker command shape:
  `docker compose run --rm -v /Users/sour4bh/.codex:/root/.codex -e GH_TOKEN="$(gh auth token)" worker ... spawnd worker --once --worker-id real-codex-worker-3`

Plan shape:

- setup command: `python -m compileall -q spawnd`
- check command: `python -m compileall -q spawnd`
- git commit enabled
- git push enabled
- artifact raw capture disabled
- telemetry exporter disabled; local trace mirror still recorded spans
- Codex config: `engine=sdk`, `sandbox=danger-full-access`,
  `approval_mode=deny_all`, `ephemeral=true`

Worker result:

```text
Worker real-codex-worker-3 finished real-contributor-20260609044421/contributor: completed
```

Postgres counts:

```text
run_id                           agents attempts events checks traces artifacts provenance
real-contributor-20260609044421       1        1      9      1     12         5          1
```

Usage:

```text
provider=openai
input_tokens=429994
output_tokens=7675
amount_usd=2.2650949999999996
source=estimated
```

Git provenance:

- base/head source commit: `d66dfa31b5e622a8cf5319748e2264d12b7af85b`
- contributor commit: `de5a4232c77212d6a9bcf1d2af43dad48c2c10fc`
- branch: `spawnd/real-contributor-20260609044421/contributor`
- changed files: `2`
- insertions: `100`
- deletions: `0`
- patch artifact id: `a3273c46467a4ea7b5509b2d210883a9`

Changed files:

- `README.md`
- `docs/deployment.md`

Final message:

```text
Implemented a narrow docs-only operations improvement.

Changed files:
- README.md: adds a pointer to the real Codex contributor deployment recipe.
- docs/deployment.md: adds a Real Codex Contributor Job section covering Codex auth mount, GitHub token handling, git askpass, plan env_refs, worker execution, and PR creation.

Verification:
- git diff --check README.md docs/deployment.md passed.
```

Check proof:

```text
command: python -m compileall -q spawnd
exit_code: 0
duration_ms: 18
```

MinIO objects:

```text
dev/runs/real-contributor-20260609044421/contributor/setup-output-8eb86c195c01462e9f38f95f578cb0ea.txt
dev/runs/real-contributor-20260609044421/contributor/runtime-output-1cfda614349a4802b283d04a100b604e.txt
dev/runs/real-contributor-20260609044421/contributor/final-message-86161cb45e764a61889dce5b201118b7.txt
dev/runs/real-contributor-20260609044421/contributor/check-output-0ceabb8c98b4480fa28202b07cd60d12.txt
dev/runs/real-contributor-20260609044421/contributor/patch-5e41bb8117df4f1c970a3de9d89ef3b2.txt
```

PR proof:

```text
remote branch: refs/heads/spawnd/real-contributor-20260609044421/contributor
remote sha: de5a4232c77212d6a9bcf1d2af43dad48c2c10fc
pr_url: https://github.com/fntune/spawnd/pull/8
pr_number: 8
headRefName: spawnd/real-contributor-20260609044421/contributor
baseRefName: main
state: OPEN
```

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

Status reconstruction after Redis flush was also verified for the real Codex
run:

```text
real-contributor-20260609044421 completed 2.2650949999999996
contributor completed spawnd/real-contributor-20260609044421/contributor 429994 7675
attempts 1
trace_span_count 12
```

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

Nightly schedules created:

```text
contributor-subport-nightly
contributor-stockbay-nightly
contributor-fn-nightly
contributor-biomon-nightly
contributor-cashgrep-nightly
```

Schedules were activated after persistent Codex/GitHub credential wiring was
verified. The activation preserved each future `next_run_at`, so no contributor
runs were submitted immediately.

```bash
docker compose exec -T api spawnd schedules set-status contributor-subport-nightly --status active
docker compose exec -T api spawnd schedules set-status contributor-stockbay-nightly --status active
docker compose exec -T api spawnd schedules set-status contributor-fn-nightly --status active
docker compose exec -T api spawnd schedules set-status contributor-biomon-nightly --status active
docker compose exec -T api spawnd schedules set-status contributor-cashgrep-nightly --status active
docker compose exec -T api spawnd schedules run-due --json
```

Result:

```text
contributor-biomon-nightly    active  2026-06-10 04:17:31.791861+00
contributor-cashgrep-nightly  active  2026-06-10 04:17:31.796403+00
contributor-fn-nightly        active  2026-06-10 04:17:31.787527+00
contributor-stockbay-nightly  active  2026-06-10 04:17:31.783407+00
contributor-subport-nightly   active  2026-06-10 04:17:31.777313+00
[]
```

Webhook receiver proof:

- `SPAWND_GITHUB_WEBHOOK_SECRET=dev-webhook-secret`
- signed GitHub-style webhook created `github-contributor-4abd61b3`
- run was cancelled through the API:
  `POST /runs/github-contributor-4abd61b3/cancel`
- GitHub `ping` events return `{"status":"pong"}` and do not submit runs, so
  hook creation checks do not start contributor work.

Result:

```text
{"cancelled":1}
github-contributor-4abd61b3 cancelled
contributor cancelled
```

Actual GitHub webhook installation on the repositories was not performed. GitHub
CLI access is now available and each target repo currently reports `0` hooks,
but this local API has no durable public callback URL. `ngrok` has no configured
local config file and `cloudflared tunnel list` has no origin certificate, so an
ephemeral tunnel would not satisfy unattended operation.

Webhook installation command added and verified in dry-run mode:

```bash
SPAWND_GITHUB_WEBHOOK_SECRET=dev-webhook-secret \
python -m spawnd.cli github-webhooks install \
  --base-url https://spawnd.example.com \
  --repo fntune/subport \
  --repo fntune/stockbay \
  --repo fntune/fn \
  --repo fntune/biomon \
  --repo fntune/cashgrep \
  --dry-run \
  --json
```

Result:

```text
fntune/subport    create  https://spawnd.example.com/webhooks/github/github-contributor
fntune/stockbay   create  https://spawnd.example.com/webhooks/github/github-contributor
fntune/fn         create  https://spawnd.example.com/webhooks/github/github-contributor
fntune/biomon     create  https://spawnd.example.com/webhooks/github/github-contributor
fntune/cashgrep   create  https://spawnd.example.com/webhooks/github/github-contributor
```

Local/non-durable callback rejection proof:

```bash
SPAWND_GITHUB_WEBHOOK_SECRET=dev-webhook-secret \
python -m spawnd.cli github-webhooks install \
  --base-url http://localhost:8765 \
  --repo fntune/subport \
  --dry-run
```

Result:

```text
Error: webhook base URL must use https
```

## OTLP Collector Proof

The local compose stack was extended with
`otel/opentelemetry-collector-contrib:0.138.0`. The worker exports OTLP over
HTTP to `http://otel-collector:4318` with
`SPAWND_TELEMETRY_FAILURE_POLICY=degrade`.

Collector and worker verification:

```bash
docker compose up -d otel-collector worker
curl -fsS http://localhost:13133/
docker compose exec -T worker sh -lc 'test "$SPAWND_TELEMETRY_ENABLED" = 1 && test "$SPAWND_TELEMETRY_EXPORTER" = otlp && test "$OTEL_EXPORTER_OTLP_ENDPOINT" = http://otel-collector:4318 && printf "telemetry=%s/%s\n" "$SPAWND_TELEMETRY_EXPORTER" "$SPAWND_TELEMETRY_FAILURE_POLICY"'
```

Result:

```text
{"status":"Server available","upSince":"2026-06-09T05:11:26.967972825Z","uptime":"58.562534087s"}
telemetry=otlp/degrade
```

OTLP export probe used Spawnd's deployed `TelemetryRecorder` from inside the
worker against the existing real run `real-contributor-20260609044421`, then
flushed the OpenTelemetry provider.

Postgres trace mirror:

```text
name                     export_status  attributes
spawnd.telemetry.probe   exported       {"probe": "otel"}
```

Collector log evidence:

```text
Traces  {"otelcol.component.id":"debug","otelcol.signal":"traces","resource spans":1,"spans":1}
```

## Worker Runtime Binary Proof

The Dockerfile links the bundled `openai-codex-cli-bin` binary onto the worker
`PATH` after installing Python dependencies.

Verification from the running worker:

```bash
docker compose exec -T worker sh -lc 'command -v codex && codex --version'
```

Result:

```text
/usr/local/bin/codex
WARNING: proceeding, even though we could not update PATH: Read-only file system (os error 30)
codex-cli 0.137.0-alpha.4
```

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

- Install real GitHub webhooks on the intended repositories after a durable
  public API callback URL is available.
