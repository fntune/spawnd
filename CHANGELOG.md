# Changelog

All notable project changes are recorded here.

This project keeps a human-written changelog. New entries should be added under
`Unreleased` first, then moved into a dated release section when pushed or
released.

## Unreleased

### Added

- Persistent compose worker auth wiring for Codex auth, GitHub tokens, git
  askpass, and the bundled Codex CLI.
- Podman deployment scripts that start infra, run migrations, and launch API,
  worker, submitter, scheduler, and outbox processes with explicit ordering.
- Reusable `deploy/templates/contributor.yaml` for conservative unattended
  contributor runs across GitHub-triggered and scheduled projects.
- Local compose OTLP collector with worker OTLP export enabled by default.
- Schedule activation controls through CLI and HTTP API.
- `spawnd github-webhooks install` for idempotent GitHub webhook create/update
  against durable public callback URLs.

## 2026-06-08 - Unattended Readiness

Commit: `68c5461 Close unattended readiness audit`

### Added

- Deployed-only execution path with Postgres as canonical state, Redis as
  coordination, and object storage for durable artifact payloads.
- Worker commands and API/CLI surfaces for run submission, status, events,
  checks, artifacts, traces, cancellation, resume, reconciliation, workers,
  queue depth, templates, schedules, webhooks, and outbox draining.
- Git delivery loop for source clone/fetch, worker worktrees, setup, commit,
  push, PR creation, PR merge, and provenance records.
- Runtime observer path for Claude, Codex, OpenAI, and fake runtimes.
- Runtime session resume support for Claude SDK sessions, OpenAI conversations,
  Codex CLI sessions, and Codex SDK threads when supported by the installed SDK.
- Internal Spawnd MCP server for Codex manager/worker coordination tools.
- HTTP bearer authentication, GitHub webhook signature validation, command
  allowlisting, secret-reference resolution, and runtime isolation policy.
- Deployment artifacts: `Containerfile`, `docker-compose.yml`, `.dockerignore`,
  deployment documentation, health/readiness/metrics endpoints, worker
  visibility, and notification webhook support.
- Alembic migration snapshot coverage and deployed schema revalidation tests.

### Changed

- Workers now claim only through Postgres transactions, renew leases while
  running, recover stale leases during polling, and treat Redis queue entries as
  wakeup hints rather than durable truth.
- Setup, runtime, check, git, PR, and artifact-producing commands are bounded by
  configured timeouts.
- Agent failure policy now supports stop, continue, retry, retry exhaustion,
  failed-dependent handling, and distinct `cost_exceeded` terminal status.
- Reviewer/read-only agents receive read-only toolsets by default.
- Codex CLI runs with JSON output, records subprocess facts, extracts session
  and token facts when emitted, and estimates cost from token usage.
- OpenAI Agents execution uses streamed results so cancellation can call
  `cancel("immediate")` before task teardown.

### Fixed

- Late worker completion/failure can no longer overwrite cancelled or terminal
  agents.
- Consumed clarification and blocker responses no longer reappear as pending.
- Runtime cancellation now reaches Codex CLI subprocesses, Claude SDK clients,
  and OpenAI streamed run results.
- Run-level cost budgets and circuit breakers are enforced before start and
  retry.
- Source repository operations use locks and explicit working directories.

### Removed

- SQLite/local durable run-state product paths and local-run compatibility
  assumptions from the deployed runtime.
- Legacy local log durability surfaces in favor of redacted object-store
  artifacts indexed from Postgres.

### Verified

- `python -m compileall -q spawnd tests`
- `git diff --check`
- `pytest -q` -> `97 passed, 50 skipped, 2 warnings`
- Product-code removal gate searches had no hits for SQLite/local-run
  durability or legacy local-mode patterns.
