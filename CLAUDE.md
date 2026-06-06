# CLAUDE.md

## System Shape

Spawnd is deployed-first agent orchestration.

- Postgres is the only durable state store.
- Redis is coordination only: queue hints, leases, heartbeats, live events, and
  cancellation notices.
- Object storage owns larger redacted payloads.
- Worker worktrees and files are execution scratch.
- CLI, Python API, and HTTP API must read and write the same deployed services.

## Ownership Tree

- `spawnd/state/`: schema, repositories, run submission, claims, leases,
  responses, events, runtime records, usage, traces, artifacts, checks,
  provenance, and queue outbox.
- `spawnd/coordination/`: Redis queues, leases, heartbeats, pubsub, and
  in-memory test doubles.
- `spawnd/artifacts/`: redaction, object-store adapters, artifact metadata.
- `spawnd/observability/`: OpenTelemetry and redacted trace mirroring.
- `spawnd/workers/`: deployed worker loop, reconciliation, setup, runtime
  execution, checks, artifacts, provenance, and dependent enqueue.
- `spawnd/runtime/`: provider runtime dispatch and observer boundary.
- `spawnd/tools/`: agent coordination tools backed by deployed events and
  responses.
- `spawnd/gitops/`: worktree, branch, diff, setup, and dependency mechanics.

Do not add a parallel state path. New state transitions belong in
`spawnd/state/repository.py` or a typed state service under `spawnd/state/`.

## Runtime Rule

Provider executors do not own durable state. They emit facts through
`RuntimeObserver` and return a result dictionary. The worker owns:

- setup invocation records;
- runtime invocation records;
- final status transitions;
- verification checks;
- redacted artifacts;
- token and cost rows;
- git provenance;
- dependent enqueue.

Codex CLI observability must stay honest: capture subprocess command preview,
cwd locator, duration, return code, output artifacts, final-message artifact,
version if available, and token metadata only when exposed. Do not invent
internal tool traces for the CLI.

## Artifact Policy

Never persist `.env` contents. Redact setup output, runtime output, final
messages, check output, provider payloads, and patch bundles unless
`orchestration.artifacts.capture_raw` is explicitly true.

Store artifact URI, hash, size, content type, kind, agent, and redaction policy
in Postgres.

## Worker Contract

Workers treat Redis queue entries as wakeups. A worker must claim an agent in a
Postgres transaction before executing. Lease expiry and queue recovery are
reconciled from Postgres.

Worker flow:

1. read Redis job hint;
2. claim in Postgres;
3. create worker scratch worktree;
4. run setup and store redacted output;
5. run runtime with observer;
6. run verification command;
7. store output, patch, usage, trace, and provenance;
8. complete or fail the agent in Postgres;
9. publish ready dependent hints through outbox plus Redis.

## Verification

Run focused tests for changed contracts and report skipped integration gates.
Postgres state tests require `SPAWND_TEST_DATABASE_URL`. Avoid adding tests that
construct an alternate durable state path.
