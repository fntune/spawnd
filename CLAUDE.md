# CLAUDE.md

## System Shape

Spawnd is deployed-first agent orchestration.

- Postgres is the only durable state store.
- Redis is coordination only: queue hints, leases, heartbeats, live events, and
  cancellation notices.
- Object storage owns larger redacted payloads.
- Worker worktrees and files are execution scratch.
- CLI, Python API, and HTTP API must read and write the same deployed services.
- A run's `source_repo` is the execution source of truth when present. It is a
  local git repository path reachable by the worker, not a remote clone request.

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
- redacted artifacts linked to the attempt, session, invocation, message, or
  tool record when that parent is known;
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
in Postgres. Link artifacts and checks to `agent_attempts`,
`runtime_sessions`, and `runtime_invocations` whenever the worker has those
ids.

Freeform redaction must catch sensitive `KEY=value` assignments, including
deployed connection strings such as `SPAWND_DATABASE_URL` and
`SPAWND_REDIS_URL`. Non-secret assignments should remain readable.

## Worker Contract

Workers treat Redis queue entries as wakeups. A worker must claim an agent in a
Postgres transaction before executing, and the claim transaction moves the run
to `running`. Lease expiry and queue recovery are reconciled from Postgres.
Queue outbox rows are recorded before Redis hints are published.

`--source-path` is only a fallback/default source. For normal submitted runs,
resolve `runs.source_repo`, require it to exist, require `git rev-parse
--show-toplevel` to succeed, and carry the canonical repository root through
worktree setup, runtime execution, checks, and provenance. Plan
`orchestration.worktree_source.base_ref` overrides `runs.source_ref`.

Worker flow:

1. read Redis job hint;
2. claim in Postgres;
3. resolve and validate the run source repository;
4. create worker scratch worktree from the resolved source and base ref;
5. run setup and store redacted output;
6. run runtime with observer;
7. run verification command;
8. store output, patch, usage, trace, and provenance;
9. complete or fail the agent in Postgres;
10. publish ready dependent hints through outbox plus Redis.

Missing source repositories, invalid git roots, and worktree creation failures
must fail the claimed agent with redacted artifacts and `runtime_errors` rows;
they must not leave the agent running.

## HTTP Contract

HTTP submit accepts an inline serialized plan only. Do not add server-local file
path reads such as `plan_file` to `POST /runs`; the CLI can read local files and
then submit the parsed plan. Validate HTTP plans at the boundary and reject
unknown request fields.

## Verification

Run focused tests for changed contracts and report skipped integration gates.
Postgres state tests require `SPAWND_TEST_DATABASE_URL`. For docs-only changes,
`git diff --check` is enough. For runtime, worker, or state changes, run the
focused deployed tests plus `python -m compileall -q spawnd tests`. Avoid
adding tests that construct an alternate durable state path.
