# Deployed Spawnd Architecture Plan

## Target Contract

Spawnd runs as a deployed orchestration system:

- Postgres is durable truth for runs, agents, attempts, events, responses,
  runtime records, artifacts, checks, traces, usage, provenance, workers, and
  queue outbox.
- Redis is a coordination plane. Queue entries and pubsub messages are wakeups,
  not durable truth.
- Object storage owns redacted payloads that are too large or too sensitive for
  direct database rows.
- Worktrees and worker files are execution scratch.
- CLI, Python API, and HTTP API are clients of the same backend.

## Execution Flow

1. `spawnd run -f plan.yaml` or API submit validates a plan and creates a run.
2. Ready agents are inserted in Postgres and published through outbox plus
   Redis queue hints.
3. Workers read hints, claim agents transactionally, and write attempts.
4. Workers create scratch worktrees from the source repo/ref.
5. Setup runs in the worktree and redacted output becomes an artifact.
6. Provider runtimes emit facts through `RuntimeObserver`.
7. Workers run verification checks uniformly.
8. Workers store redacted runtime output, final messages, checks, patches,
   usage, traces, and git provenance.
9. Completion unblocks dependent agents; reconciliation can rebuild Redis hints
   from Postgres at any time.

## Public Interfaces

- Submit: `spawnd run`, `spawnd submit`, Python `run`/`submit`, `POST /runs`.
- Workers: `spawnd worker --once`, `spawnd worker --poll`,
  `spawnd reconcile`, `spawnd worker-heartbeat`.
- Reads: `status`, `events`, `live-events`, `artifacts`, `logs`, `checks`,
  `trace`, `provenance`.
- Control: `cancel`, `resume`, `pr create`.

## Schema

The initial Alembic migration creates the complete deployed schema from
`spawnd/state/schema.py`. The table families are:

- identity and run state: `projects`, `runs`, `agents`, `agent_attempts`;
- event and response ledger: `events`, `responses`;
- runtime normalization: `runtime_sessions`, `runtime_options`,
  `runtime_invocations`, `runtime_messages`, `runtime_content_blocks`,
  `runtime_items`, `runtime_tool_calls`, `runtime_tool_results`,
  `runtime_plans`, `runtime_file_changes`;
- usage and cost: `token_usage`, `cost_usage`,
  `context_usage_snapshots`;
- permissions and tools: `runtime_permissions`, `runtime_hooks`,
  `runtime_mcp_servers`, `runtime_mcp_tools`, `runtime_subtasks`;
- provider envelope: `provider_events`;
- evidence: `artifacts`, `checks`, `git_provenance`, `trace_spans`;
- operations: `worker_nodes`, `queue_outbox`.

## Verification Gates

- Unit tests for redaction, config, parser, executor registry, and plan helpers.
- Postgres integration tests behind `SPAWND_TEST_DATABASE_URL`.
- Worker end-to-end test with fake runtime, fake Redis coordinator, and fake
  object store.
- Removal audit for deleted durability terms, deleted log helpers, and removed
  internal import paths.
