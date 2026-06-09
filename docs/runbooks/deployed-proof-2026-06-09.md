# Deployed Proof Run - 2026-06-09

This runbook records the deployed Spawnd proof performed on 2026-06-09. The
initial proof used a local Docker Compose stack; the `micro-1` continuation uses
Podman with real Postgres, Redis, MinIO, and Tailscale Funnel ingress. It
includes both an initial mock runtime proof and a later real Codex-backed
contributor run.

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

Follow-up persistent wiring added after the proof and then corrected for the
Podman host path:

- `deploy/container/git-askpass.sh` is a committed non-secret helper for git
  HTTPS auth.
- the worker image includes `gh` plus `/usr/local/bin/spawnd-git-askpass`.
- worker env loads `SPAWND_GITHUB_TOKEN` from ignored `.env` and exposes it to
  git only through `SPAWND_GIT_ASKPASS`/`GIT_ASKPASS`.
- worker mounts the required `SPAWND_CODEX_AUTH_DIR` into `/root/.codex:ro`.
- `.env.example` documents the local secret names while `.env` remains ignored.

Persistent wiring verification after recreating the worker from `.env`:

```bash
umask 077
# write SPAWND_CODEX_AUTH_DIR and SPAWND_GITHUB_TOKEN into ignored .env
deploy/podman/up.sh
podman exec -T spawnd_worker_1 sh -lc '
test -r /root/.codex/auth.json
command -v gh
gh --version | head -1
command -v spawnd-git-askpass
test "$SPAWND_GIT_ASKPASS" = /usr/local/bin/spawnd-git-askpass
test "$GIT_ASKPASS" = /usr/local/bin/spawnd-git-askpass
test -n "$SPAWND_GITHUB_TOKEN"
printf "codex_auth=readable\n"
printf "github_token=set\n"
printf "askpass=%s\n" "$SPAWND_GIT_ASKPASS"
'
podman exec -T spawnd_worker_1 sh -lc 'SPAWND_GIT_ASKPASS=/usr/local/bin/spawnd-git-askpass git ls-remote https://github.com/fntune/spawnd.git HEAD >/dev/null'
```

Result:

```text
/usr/bin/gh
gh version 2.46.0 (2025-01-13 Debian 2.46.0-3)
/usr/local/bin/spawnd-git-askpass
codex_auth=readable
github_token=set
askpass=/usr/local/bin/spawnd-git-askpass
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

Latest durable callback recheck:

```bash
env | cut -d= -f1 | rg -i 'SPAWND|PUBLIC|WEBHOOK|NGROK|CLOUDFLARE|CLOUD|TUNNEL|VERCEL|DEPLOY|DOMAIN|URL'
ngrok config check
cloudflared tunnel list
vercel whoami
vercel env ls
find . -maxdepth 4 \( -name 'vercel.json' -o -name '.vercel' -o -name 'fly.toml' -o -name 'railway.toml' -o -name 'render.yaml' -o -name 'Procfile' -o -name 'cloudflared*.yml' -o -name 'ngrok.yml' \) -print
```

Result:

```text
no public callback env vars found
ngrok config missing: /Users/sour4bh/Library/Application Support/ngrok/ngrok.yml
cloudflared origin cert missing
vercel authenticated as sour4bh, but this repo is not linked to a Vercel project
no deploy/tunnel config files found in this repo
```

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

Webhook verification command added and verified against the current missing
state:

```bash
python -m spawnd.cli github-webhooks verify \
  --base-url https://spawnd.example.com \
  --repo fntune/subport \
  --repo fntune/stockbay \
  --repo fntune/fn \
  --repo fntune/biomon \
  --repo fntune/cashgrep \
  --json
```

Result:

```text
fntune/subport    missing
fntune/stockbay   missing
fntune/fn         missing
fntune/biomon     missing
fntune/cashgrep   missing
Error: GitHub webhook verification failed
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

The container image links the bundled `openai-codex-cli-bin` binary onto the worker
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

## Micro-1 Podman Deployment

The `micro-1` deployment uses Podman, not Docker. Docker and containerd services
were stopped and verified inactive:

```text
docker.service: inactive
docker.socket: inactive
containerd.service: inactive
```

The repository now contains the Podman-owned deployment entrypoints:

- `Containerfile`
- `deploy/container/git-askpass.sh`
- `deploy/container/otel-collector.yml`
- `deploy/podman/up.sh`
- `deploy/podman/down.sh`
- `deploy/templates/contributor.yaml`

Start command used on `micro-1`:

```bash
cd /home/ubuntu/spawnd
deploy/podman/up.sh
```

The script builds `localhost/spawnd:latest`, creates the Podman network and
volumes, starts Postgres/Redis/MinIO/OTLP, waits for health, initializes the
artifact bucket, runs Alembic migrations, then starts API, submitter, scheduler,
outbox, and worker containers.

Podman service proof:

```text
spawnd_postgres_1        Up (healthy)  docker.io/library/postgres:16
spawnd_redis_1           Up (healthy)  docker.io/library/redis:7
spawnd_otel-collector_1  Up            docker.io/otel/opentelemetry-collector-contrib:0.138.0
spawnd_minio_1           Up (healthy)  docker.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
spawnd_api_1             Up            localhost/spawnd:latest
spawnd_submitter_1       Up            localhost/spawnd:latest
spawnd_scheduler_1       Up            localhost/spawnd:latest
spawnd_outbox_1          Up            localhost/spawnd:latest
spawnd_worker_1          Up            localhost/spawnd:latest
```

Local readiness:

```text
/healthz -> {"status":"ok"}
/readyz  -> {"database_configured":true,"redis_configured":true,"api_auth_configured":true}
```

Worker credential/runtime proof:

```text
/usr/local/bin/codex
codex-cli 0.137.0-alpha.4
/usr/local/bin/spawnd-git-askpass
github_token=set
git_auth=ok
```

## Tailscale Funnel And GitHub Hooks

Tailscale Funnel on `micro-1` proxies public HTTPS to the Podman API:

```text
https://micro-1.tail7a6e16.ts.net/ -> http://127.0.0.1:8765
```

Forced public-edge checks passed for both public Funnel A records:

```text
https://micro-1.tail7a6e16.ts.net/readyz -> ready
https://micro-1.tail7a6e16.ts.net/healthz -> ok
```

Signed GitHub-style webhook pings through the public Funnel edge returned:

```text
{"status":"pong"}
{"status":"pong"}
```

Installed hooks:

```text
fntune/subport   638561476  https://micro-1.tail7a6e16.ts.net/webhooks/github/github-contributor
fntune/stockbay  638561485  https://micro-1.tail7a6e16.ts.net/webhooks/github/github-contributor
fntune/fn        638561496  https://micro-1.tail7a6e16.ts.net/webhooks/github/github-contributor
fntune/biomon    638561506  https://micro-1.tail7a6e16.ts.net/webhooks/github/github-contributor
fntune/cashgrep  638561514  https://micro-1.tail7a6e16.ts.net/webhooks/github/github-contributor
```

Spawnd webhook verification returned `ok: true` for all five hooks. After
correcting the GitHub CLI path to use `/repos/...`, GitHub hook pings returned
204 and every hook reported:

```text
last_response: {"code":200,"message":"OK","status":"active"}
```

API log evidence shows the signed hook pings reaching the Podman API:

```text
POST /webhooks/github/github-contributor HTTP/1.1" 200 OK
POST /webhooks/github/github-contributor HTTP/1.1" 200 OK
POST /webhooks/github/github-contributor HTTP/1.1" 200 OK
POST /webhooks/github/github-contributor HTTP/1.1" 200 OK
POST /webhooks/github/github-contributor HTTP/1.1" 200 OK
```

## Micro-1 Templates And Schedules

The reusable contributor template is committed at
`deploy/templates/contributor.yaml` and copied into the image at
`/app/deploy/templates/contributor.yaml`. On `micro-1`, install:

```bash
podman exec spawnd_api_1 spawnd templates put github-contributor \
  -f /app/deploy/templates/contributor.yaml \
  --source-repo-template '{clone_url}' \
  --source-ref-template '{after}'
```

Project-specific templates should use the same file with fixed source repo/ref.
Recurring schedules should be created paused first so installation does not
start five Codex jobs unintentionally.

Installed templates:

```text
github-contributor
contributor-subport
contributor-stockbay
contributor-fn
contributor-biomon
contributor-cashgrep
```

Installed schedules:

```text
contributor-biomon-nightly    contributor-biomon    paused  fntune-biomon
contributor-cashgrep-nightly  contributor-cashgrep  paused  fntune-cashgrep
contributor-fn-nightly        contributor-fn        paused  fntune-fn
contributor-stockbay-nightly  contributor-stockbay  paused  fntune-stockbay
contributor-subport-nightly   contributor-subport   paused  fntune-subport
```

Template rendering was checked with a push-style parameter set:

```text
name: contributor-fntune-subport
runtime: codex
check: git diff --check
agent: contributor
```

## Micro-1 Real Runtime Continuation

The first real Codex run on `micro-1` after queue-depth validation exposed a
deployment bug:

```text
real-podman-contributor-20260609165847 failed
Codex process closed stdout ... failed to initialize sqlite state runtime under /root/.codex ... unable to open database file
```

Cause: the worker mounted `SPAWND_CODEX_AUTH_DIR` directly at `/root/.codex:ro`.
Codex 0.137.0-alpha.4 reads `auth.json` there but also creates SQLite runtime
state under the same home directory.

Fix:

- `deploy/podman/up.sh` now creates `${PROJECT}_spawnd-codex-home`.
- `seed_codex_home` copies only `auth.json` from `SPAWND_CODEX_AUTH_DIR` into
  that writable volume.
- the worker mounts `${PROJECT}_spawnd-codex-home:/root/.codex`.
- `deploy/podman/down.sh --volumes` removes the Codex home volume with the rest
  of deployed state.

Validation:

```bash
bash -n deploy/podman/up.sh deploy/podman/down.sh
git diff --check
rsync -az --delete --exclude .git --exclude .env --exclude .venv \
  --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
  /Users/sour4bh/dev/spawnd/ micro-1:/home/ubuntu/spawnd/
ssh micro-1 'cd /home/ubuntu/spawnd && SPAWND_PODMAN_SKIP_BUILD=1 deploy/podman/up.sh'
ssh micro-1 'podman exec spawnd_worker_1 sh -lc '"'"'test -w /root/.codex && test -r /root/.codex/auth.json && codex --version'"'"''
```

Result:

```text
codex_home_writable
codex_auth_seeded
codex-cli 0.137.0-alpha.4
```

Successful real Codex-backed contributor run:

```text
run_id: real-podman-contributor-20260609170708
source_repo: https://github.com/fntune/spawnd.git
source_ref: origin/main
worker: podman-worker-1
runtime: codex sdk
status: completed
input_tokens: 63317
output_tokens: 976
cost_usd: 0.331225
```

Plan shape:

- setup: `python -m compileall -q spawnd`
- check: `git diff --check`
- git commit: enabled
- git push: enabled
- raw artifact capture: disabled
- telemetry exporter: OTLP with degrade failure policy
- cleanup: disabled for this proof so `spawnd pr create` could use the
  recorded worktree path

Postgres evidence:

```text
runs agents attempts events checks traces artifacts provenance sessions invocations token_rows cost_rows
1    1      1        9      1      12     5         1          1        3           1          1
```

Runtime records:

```text
setup   completed
runtime completed
check   completed
```

Usage records:

```text
token_usage: provider=openai input_tokens=63317 output_tokens=976
cost_usage:  provider=openai amount_usd=0.331225 source=estimated
```

Artifact objects in MinIO:

```text
dev/runs/real-podman-contributor-20260609170708/contributor/setup-output-15cce008ed66407caf08bd18af50a903.txt
dev/runs/real-podman-contributor-20260609170708/contributor/runtime-output-4a9dd2ab220b4c8fa69d8f0111d37a3d.txt
dev/runs/real-podman-contributor-20260609170708/contributor/final-message-cd3c340e39f744ddaa067dee6f4fe65f.txt
dev/runs/real-podman-contributor-20260609170708/contributor/check-output-110edd0b47334295a276aa56aeb4dd1d.txt
dev/runs/real-podman-contributor-20260609170708/contributor/patch-53f659867d9141a89c3e5d9bf41514a2.txt
```

Artifact-backed logs:

```text
Updated docs/deployment.md with a concise operator note clarifying that
queue_depth and submission_queue_depth are Redis consumer-group backlog, not raw
stream history, and that acknowledged entries can remain while backlog is zero.

Verification: git diff --check -- docs/deployment.md passed.
Only docs/deployment.md is modified.
```

Git provenance:

```text
base_sha: c6a0bda7b0db9f1c0604f0e19599a0ffbd9404b6
commit_sha: a9b06dd4698bf33ef3e1f345588da4e630c49ba0
branch: spawnd/real-podman-contributor-20260609170708/contributor
changed files: 1
insertions: 4
deletions: 0
patch_artifact_id: 6df99a23df0546f9bfa11bd9f6f25f6b
```

Remote branch proof:

```text
a9b06dd4698bf33ef3e1f345588da4e630c49ba0 refs/heads/spawnd/real-podman-contributor-20260609170708/contributor
```

PR creation command:

```bash
ssh micro-1 'podman exec spawnd_worker_1 sh -lc '"'"'export GH_TOKEN="$SPAWND_GITHUB_TOKEN"; spawnd pr create real-podman-contributor-20260609170708 --agent contributor --title-prefix spawnd-real-podman --timeout-seconds 120'"'"''
```

Result:

```text
https://github.com/fntune/spawnd/pull/9
```

GitHub PR proof:

```text
number: 9
state: OPEN
baseRefName: main
headRefName: spawnd/real-podman-contributor-20260609170708/contributor
headRefOid: a9b06dd4698bf33ef3e1f345588da4e630c49ba0
files: docs/deployment.md +4 -0
```

Postgres provenance was updated with:

```text
pr_url: https://github.com/fntune/spawnd/pull/9
pr_number: 9
```

Redis-loss reconstruction proof:

```bash
ssh micro-1 'podman exec spawnd_redis_1 redis-cli flushdb'
ssh micro-1 'podman exec spawnd_api_1 spawnd status real-podman-contributor-20260609170708 --json'
ssh micro-1 'podman exec spawnd_api_1 spawnd provenance real-podman-contributor-20260609170708 --json'
ssh micro-1 'podman exec spawnd_api_1 spawnd workers --json'
```

Result:

```text
redis flushdb: OK
run status: completed
provenance: PR #9 and commit a9b06dd4698bf33ef3e1f345588da4e630c49ba0 reconstructed from Postgres
queue_depth: 0
submission_queue_depth: 0
podman-worker-1: active, stale=false
```

Cancellation proof:

```text
run_id: cancel-podman-20260609171507
command: spawnd cancel cancel-podman-20260609171507
one-shot worker: spawnd worker --once --mock --worker-id cancel-proof-worker --block-ms 1000
```

Result:

```text
Cancelled run cancel-podman-20260609171507
Agents cancelled: 1
Worker cancel-proof-worker found no ready agents
run status: cancelled
agent status: cancelled
attempts: 0
events: run_created, run_cancelled
```

Retry/recovery proof:

```text
run_id: retry-podman-20260609171621
attempt 1: claimed by lost-worker
lease action: agents and agent_attempts leased_until aged 10 minutes into the past
reconcile: Requeued hints: 1
attempt 2: completed by retry-proof-worker with --mock against deployed Postgres/Redis/S3
```

Result:

```text
run status: completed
retry_attempt: 1
attempt 1: expired, worker_id=lost-worker
attempt 2: completed, worker_id=retry-proof-worker
events: run_created, agent_claimed, lease_expired, agent_claimed, worktree_source_resolved, started, done, worktree_cleaned
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

The durable public callback blocker is closed on `micro-1`: Podman API is
reachable through Tailscale Funnel and GitHub reports all five hooks active with
HTTP 200 pings. A real Codex-backed contributor run completed through deployed
Spawnd and produced PR #9. Remaining operational choices are intentional
activation of the five paused recurring schedules and whether to trigger the
first real GitHub push/PR webhook contributor run on one of the product repos.
