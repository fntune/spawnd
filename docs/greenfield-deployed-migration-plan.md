# Greenfield Deployed Migration

This repository now targets one deployed execution model.

## Final Shape

- `spawnd/state` owns the durable schema and repository services.
- `spawnd/coordination` owns Redis queues, leases, heartbeats, live events, and
  cancellation notices.
- `spawnd/artifacts` owns redaction plus object-store writes.
- `spawnd/observability` owns OpenTelemetry and trace mirroring.
- `spawnd/workers` owns deployed execution, setup, checks, artifacts,
  provenance, and dependent enqueue.
- `spawnd/runtime` owns provider invocation and the observer boundary.
- `spawnd/tools` records coordination facts through deployed events and
  responses.

## Durable Record

Durable run evidence is:

- Postgres rows for state transitions and queryable provenance;
- submitted `source_repo` and `source_ref` fields that identify the worker-local
  git source path and default worktree base ref;
- object-store artifacts for large redacted payloads;
- git provenance rows and patch artifacts for code changes;
- trace rows plus optional OTLP export for observability.

Worker worktrees remain execution scratch and are not durable evidence.

## Current Execution Contract

- `source_repo` is a local git repository path reachable by the worker. It is
  validated before worktree creation and is not cloned from a remote URL.
- `source_ref` is the submitted default base ref. A plan-level
  `orchestration.worktree_source.base_ref` overrides it.
- `--source-path` on the worker is a fallback source for runs without
  `source_repo`, not the primary routing mechanism.
- Missing source repositories, invalid git roots, and worktree creation failures
  fail the claimed agent with redacted artifacts and `runtime_errors` rows.
- Artifacts and checks are linked to attempts, runtime sessions, and
  invocations whenever those parent ids are available.

## Migration Gates

- CLI and Python API submit to the same backend.
- HTTP API exposes submit, status, events, checks, artifacts, traces, cancel,
  resume, and reconciliation.
- HTTP submit accepts a serialized plan body only. It validates the plan at the
  boundary, rejects unknown fields, and does not read server-local plan files.
- Runtimes emit facts through `RuntimeObserver`.
- Workers claim in Postgres before execution and reconcile Redis hints from
  Postgres.
- Claims move runs to `running`; cancellation and lease expiry refresh aggregate
  run state from Postgres.
- Reconciliation records queue outbox rows before publishing Redis wakeups.
- Tests that exercise state transitions use `SPAWND_TEST_DATABASE_URL`.

## Remaining Operational Gaps

- Remote clone/fetch and scoped git credentials are not implemented. Worker
  hosts must already have the submitted repositories and refs available.
- Completed agent branches are recorded as provenance and patch artifacts but
  are not pushed automatically.
- Runtime wall-clock deadlines, retryable provider error classification,
  deployment manifests, API auth, and worktree cleanup are still separate
  unattended-operation work.
