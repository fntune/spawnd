# Unattended Readiness Revalidation

Date: 2026-06-08

This revalidates `docs/audits/2026-06-07-unattended-readiness.md` against the
current deployed-only implementation. The June 7 audit is retained as the
baseline; this file records the current closure state.

## Verification

- `python -m compileall -q spawnd tests`
- `git diff --check`
- `pytest -q`
- Result: `97 passed, 50 skipped, 2 warnings`
- Product-code removal gate searches had no hits for SQLite/local-run durability
  patterns, local log durability helpers, or legacy/local-mode compatibility
  wording.

## Extra Revalidation Bugs

| Finding | Status | Evidence |
| --- | --- | --- |
| Late terminal writes can overwrite cancelled/terminal agents | Resolved | `complete_agent` / `fail_agent` require a matching running attempt and do not rewrite terminal agents in `spawnd/state/repository.py`; covered by deployed repository tests. |
| Consumed clarification/blocker responses can resurface pending | Resolved | Pending response queries exclude consumed responses in `spawnd/state/repository.py`; covered by deployed repository tests. |

## Finding Status

| ID | Status | Current evidence |
| --- | --- | --- |
| G00 | Resolved | Runtime/setup/check timeouts are plan fields and are enforced in `spawnd/workers/worker.py`; Codex CLI subprocesses are bounded and killed on timeout in `spawnd/runtime/executors/codex.py`. |
| G01 | Resolved | Git delivery can commit/push branches and `spawnd pr create` creates PRs from recorded provenance in `spawnd/workers/worker.py` and `spawnd/cli.py`. |
| G02 | Resolved | Retryable provider/runtime errors are classified and passed into `fail_agent`; retry policy is enforced in `spawnd/state/repository.py`. |
| G04 | Resolved | Authenticated GitHub webhook submission exists at `/webhooks/github/{template_id}` with HMAC validation in `spawnd/server.py`. |
| G05 | Resolved | Workers resolve `runs.source_repo`, clone/fetch remote URLs, and use worker-local source caches in `spawnd/workers/worker.py`. |
| G06 | Resolved | `worker --poll` expires stale leases before polling and requeues from Postgres in `spawnd/workers/worker.py`. |
| G07 | Resolved | `on_failure='retry'` and `retry_count` requeue agents automatically through `fail_agent`. |
| G08 | Resolved | `on_failure='stop'` terminalizes non-terminal agents and updates run state in `spawnd/state/repository.py`. |
| G09 | Resolved | Failed dependencies are marked terminal or skipped according to failure policy; runs now roll up to terminal states. |
| G10 | Resolved | Codex CLI subprocesses are killed on timeout/cancellation, Claude SDK cancellation calls `client.interrupt()` before task teardown, and OpenAI Agents runs through the streamed result so cancellation calls `cancel("immediate")` on the provider result before task teardown. |
| G12 | Resolved | `spawnd pr merge` records merge events and supports merge/squash/rebase through `gh`. |
| G13 | Resolved | Agent work is committed by default and dependent worktrees merge or import upstream dependency branches in `spawnd/gitops/worktrees.py`. |
| G14 | Resolved | `orchestration.cleanup.worktree` enables terminal worktree cleanup through `remove_worktree`. |
| G15 | Resolved | HTTP routes require bearer auth via `SPAWND_API_TOKEN` except health/readiness/metrics and signed webhooks. |
| G16 | Resolved | Write-capable real runtimes fail before setup/provider execution unless the worker declares `SPAWND_RUNTIME_ISOLATION=container/jail/vm`; compose workers declare container isolation. Default agent shell is removed and readonly agents are wired. |
| G17 | Resolved | Agent, git, and MCP secrets use explicit `env_refs` resolved at worker runtime. |
| G18 | Resolved | Raw `agent.env` is stripped from durable run specs and replaced with redacted metadata. |
| G19 | Resolved | Setup/check commands are validated through `orchestration.command_policy`; PR/git commands have explicit timeouts. |
| G20 | Resolved | Source and git delivery operations resolve credential refs from the worker environment. |
| G21 | Resolved | Dockerfile, compose stack, deployment docs, and server/worker/submitter entrypoints are present. |
| G22 | Resolved | Polling workers catch transient loop errors, expire/requeue stale work, and continue polling. |
| G23 | Resolved | Notification webhook delivery records sent/error events for failures, budget stops, stuck work, and PR creation. |
| G24 | Resolved | Run cost budgets and circuit breakers are enforced in repository state transitions. |
| G26 | Resolved | `/healthz`, `/readyz`, `/metrics`, worker listing, and queue-depth visibility are implemented. |
| G27 | Resolved | `0001` is frozen to a historical schema snapshot, `0002` carries current additions, and migration tests compare Alembic head to state metadata. |
| G32 | Resolved | Source-repo git operations use locks; `_pushd` is a no-op and worktree/runtime commands pass explicit cwd values. |
| G34 | Resolved | Codex SDK cost is estimated from exposed token counts; Codex CLI runs with `--json` and estimates cost from emitted token usage instead of always reporting `0.0`. |
| G36 | Resolved | Per-agent Codex engine/sandbox/approval/ephemeral config is modeled and applied. |
| G39 | Resolved | Manager coordination tools create real queued dynamic agents and update durable run specs. |
| G41 | Resolved | Durable schedules and due-run submission are implemented. |
| G42 | Resolved | Durable run templates and parameter rendering are implemented. |
| G43 | Resolved | Redis-backed submission queue ingress and drainer are implemented. |
| G44 | Resolved | Queue outbox rows are drainable and reconciler/outbox paths are wired to Redis publishing. |
| G45 | Resolved | `cost_exceeded` is a distinct terminal status and is not retried as a generic failure. |
| G46 | Resolved | Redaction covers assignments, JSON secret fields, bearer tokens, URL credentials, and bare provider keys. |
| G47 | Resolved | Reviewer/readonly agents persist `write_allowed=false` and receive read-only runtime toolsets. |
| G48 | Resolved | Worker/queue visibility and run-level concurrency limits are wired through repository, CLI, and HTTP surfaces. |
| G49 | Resolved | Setup cache keys include command/source/lockfile inputs and workers provide common npm/pnpm/yarn/bun/uv/pip/poetry cache directories to setup commands. |
| G50 | Resolved | Claude and OpenAI accept configured MCP servers through SDK-native APIs; Codex CLI accepts supported stdio/HTTP MCP servers through per-run config overrides and rejects unsupported MCP shapes at validation. All configured servers are recorded in `runtime_mcp_servers`. |
| G51 | Resolved | Claude resumes via SDK session id, OpenAI resumes via conversation id, Codex CLI resumes with `codex exec resume`, and Codex SDK uses `thread_resume`/`resume_thread` when a stored thread id exists. Installed Codex SDKs without a resume method fail clearly instead of cold-starting. |
| G52 | Resolved | Codex manager agents execute through the CLI engine with the internal Spawnd MCP server, exposing the same durable manager coordination tools used by Claude/OpenAI managers. |
| G53 | Resolved | `live-events` uses Redis pubsub with Postgres replay/reconstruction as fallback. |

## Remaining Work

No findings from the June 7 audit remain open in this revalidation.
