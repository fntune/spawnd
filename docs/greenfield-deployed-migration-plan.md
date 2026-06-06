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
- object-store artifacts for large redacted payloads;
- git provenance rows and patch artifacts for code changes;
- trace rows plus optional OTLP export for observability.

Worker worktrees remain execution scratch and are not durable evidence.

## Migration Gates

- CLI and Python API submit to the same backend.
- HTTP API exposes submit, status, events, checks, artifacts, traces, cancel,
  resume, and reconciliation.
- Runtimes emit facts through `RuntimeObserver`.
- Workers claim in Postgres before execution and reconcile Redis hints from
  Postgres.
- Tests that exercise state transitions use `SPAWND_TEST_DATABASE_URL`.
