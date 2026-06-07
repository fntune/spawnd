# Deployed Observability Schema Plan

This document maps the Codex and Claude runtime facts spawnd can collect into a
normalized Postgres schema for deployed runs. The greenfield deployed schema now
includes the durable tables for runs, agents, attempts, runtime sessions,
invocations, messages, content blocks, items, tools, hooks, permissions, MCP,
subtasks, provider events, usage, errors, artifacts, checks, traces, git
provenance, workers, and queue outbox. The remaining work is deeper provider
ingestion into these tables, not a second local durability path.

The goal is not to reproduce every vendor transcript verbatim in Postgres.
Postgres should hold durable, queryable provenance and state. Large or sensitive
content belongs in redacted object-store artifacts, linked from normalized rows
by hash, size, content type, and redaction policy.

## Sources Checked

- Local package: `openai-codex==0.1.0b2`
- Local package: `claude-agent-sdk==0.2.87`
- OpenAI Codex Python SDK package metadata: <https://pypi.org/project/openai-codex/>
- Claude Agent SDK overview: <https://code.claude.com/docs/en/agent-sdk/overview>
- Claude Agent SDK permissions: <https://code.claude.com/docs/en/agent-sdk/permissions>
- Claude Agent SDK Python repository: <https://github.com/anthropics/claude-agent-sdk-python>

## Collection Boundaries

Every collected fact should be classified before it is stored:

- `sdk_fact`: a typed SDK field such as `turn_id`, `session_id`, `duration_ms`,
  `model`, `status`, `usage`, or `cost`.
- `spawnd_fact`: orchestration state such as `run_id`, `agent_name`,
  `worker_id`, `lease_token`, retry attempt, submitted source repository,
  source ref, resolved worktree base ref, branch, worktree locator, check
  command hash, and artifact ids.
- `derived_fact`: redacted preview, content hash, prompt hash, diff stats, cost
  estimates, token totals, or status rollups.
- `raw_payload`: provider wire shape or runtime output retained only as a
  redacted object artifact by default. Postgres may keep a compact redacted JSON
  envelope for replay/debugging, but not full logs or prompts.

Codex CLI has a narrower observability contract than Codex SDK. In CLI mode,
spawnd can honestly capture subprocess command metadata, duration, exit code,
stdout/stderr artifacts, final-message hash/size/artifact, and environment
metadata. It should not invent internal tool calls, turns, token usage, or
reasoning spans that the CLI boundary did not expose.

Run source is also part of the provenance boundary. `runs.source_repo` is a
worker-local git repository path that must already exist on the worker host.
`runs.source_ref` is the submitted default worktree base. Plan
`orchestration.worktree_source.base_ref` intentionally overrides `source_ref`.
Remote clone-on-demand and scoped git credentials are future operational work,
not part of the current source contract.

## Codex Runtime Mapping

Codex SDK exposes a high-level `AsyncCodex` thread/turn API and lower-level
notifications. The currently used high-level path can collect the following
stable facts.

| Area | SDK fields or events | Store as |
| --- | --- | --- |
| SDK process | `AsyncCodex.metadata.serverInfo`, `userAgent`, `platformFamily`, `platformOs` | `runtime_sessions.metadata` |
| Thread start | `thread.id`; start options `cwd`, `ephemeral`, `model`, `model_provider`, `approval_mode`, `sandbox`, `service_tier` | `runtime_sessions`, `agent_attempts` |
| Thread read/start response | `Thread.cliVersion`, `createdAt`, `updatedAt`, `cwd`, `ephemeral`, `gitInfo`, `modelProvider`, `sessionId`, `source`, `status`, `threadSource` | `runtime_sessions.provider_metadata` and selected columns |
| Turn | `TurnResult.id`, `status`, `error`, `started_at`, `completed_at`, `duration_ms`, `items`, `final_response`, `usage` | `runtime_invocations`, `runtime_messages`, `artifacts`, `token_usage` |
| Token usage | `ThreadTokenUsage.last` and `total`: `cachedInputTokens`, `inputTokens`, `outputTokens`, `reasoningOutputTokens`, `totalTokens`; optional `modelContextWindow` | `token_usage` with `scope='turn_last'` and `scope='thread_total'` |
| Message deltas | `AgentMessageDeltaNotification(delta, itemId, threadId, turnId)` | append to redacted stream artifact; optionally index byte/count metadata |
| Items | `ItemStartedNotification`, `ItemCompletedNotification` with `ThreadItem`, start/complete timestamps | `runtime_items`, `runtime_messages`, `runtime_tool_calls` depending on item type |
| Raw Responses item | `RawResponseItemCompletedNotification.item` | `provider_events` plus redacted artifact if large |
| Plans and diffs | `TurnPlanUpdatedNotification`, `PlanDeltaNotification`, `TurnDiffUpdatedNotification.diff` | `runtime_plans`, `artifacts(kind='diff')` |
| Terminal and command output | `TerminalInteractionNotification`, `CommandExecutionOutputDeltaNotification` | `runtime_tool_calls`, output artifacts |
| File changes | `FileChangeOutputDeltaNotification` | `runtime_file_changes`, patch artifact |
| Errors | `ErrorNotification`, `TurnError.message`, `additionalDetails`, `codexErrorInfo` | `runtime_errors`, span status, event ledger |

For Codex CLI mode, collect only:

- executable path and version if available;
- argv hash and redacted argv preview;
- cwd/worktree locator;
- environment key metadata and redacted values;
- start/end timestamps, duration, return code, signal if available;
- stdout/stderr redacted artifacts;
- `--output-last-message` content hash, size, preview, and artifact;
- check command facts and artifacts;
- git provenance after execution.

## Claude Runtime Mapping

Claude Agent SDK exposes a stream of typed `Message` objects plus options,
hooks, permission events, MCP status, context usage, and result metadata.
Spawnd currently records coarse session, invocation, usage, final-message, and
error facts through the observer boundary. The normalized tables below are the
target home for richer message, hook, permission, MCP, and tool-result
ingestion.

| Area | SDK fields or events | Store as |
| --- | --- | --- |
| Options | `cwd`, `model`, `fallback_model`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `tools`, `mcp_servers`, `strict_mcp_config`, `max_turns`, `max_budget_usd`, `setting_sources`, `skills`, `plugins`, `sandbox`, `thinking`, `effort` | `runtime_sessions`, `runtime_options`, redacted artifacts for large config |
| User message | `UserMessage.content`, `uuid`, `parent_tool_use_id`, `tool_use_result` | `runtime_messages`, `runtime_content_blocks`, artifact for full content |
| Assistant message | `AssistantMessage.content`, `model`, `parent_tool_use_id`, `error`, `usage`, `message_id`, `stop_reason`, `session_id`, `uuid` | `runtime_messages`, `runtime_content_blocks`, `token_usage`, `runtime_errors` |
| Text content | `TextBlock.text` | redacted artifact plus preview/hash in `runtime_content_blocks` |
| Thinking content | `ThinkingBlock.thinking`, `signature` | do not store raw by default; store hash/signature metadata if enabled |
| Tool use | `ToolUseBlock.id`, `name`, `input` | `runtime_tool_calls` with redacted input artifact |
| Tool result | `ToolResultBlock.tool_use_id`, `content`, `is_error` | `runtime_tool_results`, redacted output artifact |
| Server tool use/result | `ServerToolUseBlock`, `ServerToolResultBlock` | `runtime_tool_calls` with `tool_origin='server'` |
| Task messages | `TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage` with task id, description, status, output file, summary, usage | `runtime_subtasks`, `token_usage`, artifacts |
| Hook lifecycle | `HookEventMessage`, `include_hook_events`, hook input/output types such as `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop`, `SubagentStart`, `SubagentStop` | `runtime_hooks`, `runtime_permissions`, event ledger |
| Permission callback | `can_use_tool(tool_name, input, context)` and returned allow/deny result | `runtime_permissions` |
| MCP status | `get_mcp_status()` server name/status/info/error/config/tools | `runtime_mcp_servers`, `runtime_mcp_tools` |
| Context usage | `get_context_usage()` categories, totals, model, memory files, MCP tools, agents, API usage | `context_usage_snapshots` |
| Final result | `ResultMessage.subtype`, `duration_ms`, `duration_api_ms`, `is_error`, `num_turns`, `session_id`, `stop_reason`, `total_cost_usd`, `usage`, `result`, `structured_output`, `model_usage`, permission denials, deferred tool use, errors, `api_error_status`, `uuid` | `runtime_invocations`, `cost_usage`, `token_usage`, `runtime_errors`, artifacts |

Claude hook coverage matters for production reliability. The permission docs
make `allowed_tools` an allow rule, not a tool availability boundary. For
audit-grade records, collect hook and permission decisions separately from
message/tool-call facts.

## Normalized Postgres Target

The current deployed schema already contains the durable foundation and the
first normalized runtime execution tables. Keep `events` as the append-only
ledger, but do not make it the only query path for common observability
questions. New provider details should land in the narrowest normalized table
available and fall back to `provider_events` plus redacted artifacts when the
vendor shape is still unstable.

### Identity And Ownership

`projects`

- `id uuid primary key`
- `slug text unique not null`
- `source_repo text`
- `created_at timestamptz not null`

`runs`

- current table includes `project_id`, `tenant_id`, `idempotency_key`,
  `submitted_by`, `submitted_via`, `spec_hash`, `spec_artifact_id`,
  `source_repo`, `source_ref`, `cancelled_at`, and `finished_at`
- keep full plan spec as an object artifact if it can contain sensitive prompt
  or env content

`agents`

- existing table, keep as current materialized state
- write per-attempt mutable execution facts to `agent_attempts`
- keep `input_tokens`, `output_tokens`, and `cost_usd` only as rollups

`agent_attempts`

- `id uuid primary key`
- `run_id`, `agent_name`
- `attempt_number int`
- `runtime text`
- `model text`
- `status text`
- `worker_id text`
- `lease_token text`
- `leased_until timestamptz`
- `heartbeat_at timestamptz`
- `started_at`, `finished_at`
- `error_id uuid`
- unique `(run_id, agent_name, attempt_number)`

This table is the natural parent for SDK sessions, invocations, checks, and
artifacts because retry attempts must not overwrite each other.

### Runtime Sessions And Invocations

`runtime_sessions`

- `id uuid primary key`
- `attempt_id uuid not null`
- `provider text not null` (`anthropic`, `openai`)
- `runtime text not null` (`claude_sdk`, `codex_sdk`, `codex_cli`, `openai_agents`)
- `provider_session_id text`
- `provider_thread_id text`
- `provider_account_hash text`
- `cwd_locator text`
- `model text`
- `model_provider text`
- `sdk_package text`
- `sdk_version text`
- `cli_version text`
- `auth_mode text`
- `ephemeral boolean`
- `permission_mode text`
- `sandbox_policy text`
- `metadata jsonb not null default '{}'`
- unique provider ids where present

`runtime_options`

- `id uuid primary key`
- `session_id uuid not null`
- `option_name text not null`
- `option_kind text not null` (`model`, `permission`, `sandbox`, `tool`,
  `mcp`, `context`, `budget`, `provider`, `env`, `other`)
- `value_hash text`
- `value_preview text`
- `value_artifact_id uuid`
- `source text` (`plan`, `default`, `environment`, `worker`, `sdk`)
- unique `(session_id, option_name, source)`

Use this for provider/runtime knobs that are important for audit but too
variable to deserve a first-class column on `runtime_sessions`.

`runtime_invocations`

- `id uuid primary key`
- `session_id uuid not null`
- `attempt_id uuid not null`
- `provider_turn_id text`
- `provider_request_id text`
- `sequence int not null`
- `kind text not null` (`turn`, `subprocess`, `check`, `setup`, `resume`)
- `status text not null`
- `started_at`, `completed_at`
- `duration_ms int`
- `api_duration_ms int`
- `exit_code int`
- `signal text`
- `stop_reason text`
- `is_error boolean not null default false`
- `error_id uuid`
- `final_message_artifact_id uuid`
- `final_message_hash text`
- unique `(session_id, sequence)`
- unique `(session_id, provider_turn_id)` when present

Use this for Codex `TurnResult`, Claude `ResultMessage`, Codex CLI subprocesses,
worktree setup, and verification commands.

### Messages, Blocks, And Tools

`runtime_messages`

- `id uuid primary key`
- `invocation_id uuid not null`
- `provider_message_id text`
- `provider_uuid text`
- `session_provider_id text`
- `role text not null` (`user`, `assistant`, `system`, `result`)
- `subtype text`
- `sequence int not null`
- `parent_tool_call_id uuid`
- `model text`
- `status text`
- `stop_reason text`
- `error_id uuid`
- `content_artifact_id uuid`
- `content_hash text`
- `content_preview text`
- unique `(invocation_id, sequence)`

`runtime_content_blocks`

- `id uuid primary key`
- `message_id uuid not null`
- `sequence int not null`
- `block_type text not null` (`text`, `thinking`, `tool_use`, `tool_result`,
  `server_tool_use`, `server_tool_result`, `raw_response_item`, `unknown`)
- `provider_block_id text`
- `tool_call_id uuid`
- `content_hash text`
- `content_preview text`
- `content_artifact_id uuid`
- `metadata jsonb not null default '{}'`

`runtime_items`

- `id uuid primary key`
- `invocation_id uuid not null`
- `provider_item_id text`
- `provider_thread_id text`
- `provider_turn_id text`
- `item_type text not null`
- `phase text`
- `started_at timestamptz`
- `completed_at timestamptz`
- `content_hash text`
- `content_artifact_id uuid`
- `metadata jsonb not null default '{}'`
- unique `(invocation_id, provider_item_id)` where provider id is present

Codex `ThreadItem` data should land here first, then be promoted to
`runtime_messages`, `runtime_tool_calls`, or `runtime_file_changes` when the
item type has stable product meaning.

`runtime_tool_calls`

- `id uuid primary key`
- `invocation_id uuid not null`
- `message_id uuid`
- `provider_tool_use_id text`
- `tool_origin text not null` (`builtin`, `mcp`, `server`, `spawnd_coord`,
  `subagent`, `unknown`)
- `tool_name text not null`
- `mcp_server text`
- `input_hash text`
- `input_artifact_id uuid`
- `permission_id uuid`
- `started_at`, `completed_at`
- `status text`
- `error_id uuid`

`runtime_tool_results`

- `id uuid primary key`
- `tool_call_id uuid not null`
- `provider_tool_use_id text`
- `is_error boolean`
- `output_hash text`
- `output_artifact_id uuid`
- `summary_preview text`
- `metadata jsonb not null default '{}'`

This lets the product answer basic questions without parsing provider JSON:
which tools ran, which were blocked, which commands failed, and which artifact
holds the output.

`runtime_plans`

- `id uuid primary key`
- `invocation_id uuid not null`
- `provider_item_id text`
- `sequence int not null`
- `step text`
- `status text`
- `explanation_hash text`
- `explanation_preview text`
- `delta_artifact_id uuid`
- `created_at timestamptz not null`

Use this for Codex plan updates and any future provider plan/progress contract
that is meant to be shown as structured progress.

`runtime_file_changes`

- `id uuid primary key`
- `invocation_id uuid not null`
- `tool_call_id uuid`
- `path_hash text not null`
- `path_preview text`
- `change_kind text`
- `diff_artifact_id uuid`
- `patch_artifact_id uuid`
- `started_at timestamptz`
- `completed_at timestamptz`
- `metadata jsonb not null default '{}'`

### Usage, Cost, And Context

`token_usage`

- `id uuid primary key`
- `run_id`, `agent_name`, `attempt_id`, `session_id`, `invocation_id`,
  `message_id`
- `provider text not null`
- `model text`
- `scope text not null` (`message`, `turn_last`, `thread_total`,
  `result_total`, `task`, `context_snapshot`)
- `input_tokens int`
- `cached_input_tokens int`
- `output_tokens int`
- `reasoning_output_tokens int`
- `total_tokens int`
- `context_window int`
- `raw_usage jsonb not null default '{}'`
- `created_at timestamptz not null`

`cost_usage`

- `id uuid primary key`
- `attempt_id`, `session_id`, `invocation_id`
- `provider text not null`
- `model text`
- `amount_usd numeric(12, 6)`
- `source text not null` (`sdk`, `estimated`, `manual`, `unknown`)
- `raw_cost jsonb not null default '{}'`
- `created_at timestamptz not null`

`context_usage_snapshots`

- `id uuid primary key`
- `session_id uuid not null`
- `invocation_id uuid`
- `total_tokens int`
- `max_tokens int`
- `raw_max_tokens int`
- `percentage numeric(6, 3)`
- `model text`
- `is_auto_compact_enabled boolean`
- `categories jsonb not null`
- `memory_files jsonb not null`
- `mcp_tools jsonb not null`
- `agents jsonb not null`
- `api_usage jsonb`
- `created_at timestamptz not null`

Do not collapse usage into `agents` only. `agents` can keep rollups, but the
source rows need scope and provenance so vendor schema drift is visible.

### Permissions, Hooks, MCP, And Subagents

`runtime_permissions`

- `id uuid primary key`
- `invocation_id uuid not null`
- `tool_call_id uuid`
- `provider_tool_use_id text`
- `source text not null` (`mode`, `allow_rule`, `deny_rule`, `can_use_tool`,
  `hook`, `default`)
- `permission_mode text`
- `decision text not null` (`allow`, `deny`, `ask`, `defer`, `block`)
- `reason_hash text`
- `reason_preview text`
- `updated_input_artifact_id uuid`
- `created_at timestamptz not null`

`runtime_hooks`

- `id uuid primary key`
- `invocation_id uuid not null`
- `provider_hook_event text not null`
- `phase text not null` (`started`, `response`, `failure`)
- `tool_call_id uuid`
- `provider_tool_use_id text`
- `agent_provider_id text`
- `input_hash text`
- `input_artifact_id uuid`
- `output_hash text`
- `output_artifact_id uuid`
- `duration_ms int`
- `status text`
- `created_at timestamptz not null`

`runtime_mcp_servers`

- `id uuid primary key`
- `session_id uuid not null`
- `name text not null`
- `status text not null`
- `server_name text`
- `server_version text`
- `scope text`
- `config_hash text`
- `config_artifact_id uuid`
- `error_id uuid`
- unique `(session_id, name)`

`runtime_mcp_tools`

- `id uuid primary key`
- `mcp_server_id uuid not null`
- `name text not null`
- `description_hash text`
- `annotations jsonb not null default '{}'`
- unique `(mcp_server_id, name)`

`runtime_subtasks`

- `id uuid primary key`
- `invocation_id uuid not null`
- `provider_task_id text not null`
- `provider_agent_id text`
- `task_type text`
- `description_hash text`
- `description_preview text`
- `status text`
- `output_artifact_id uuid`
- `summary_hash text`
- `summary_preview text`
- `started_at`, `completed_at`
- unique `(invocation_id, provider_task_id)`

### Artifacts, Checks, Git, And Errors

The current `artifacts`, `checks`, and `git_provenance` tables are the deployed
read model for output, verification, and code-change provenance. Keep them
linked to attempts, sessions, and invocations as those ids become available.

`artifacts`

- UUID primary key for cross-service references
- parent links: `attempt_id`, `session_id`, `invocation_id`, `message_id`,
  `tool_call_id` where known
- storage locator: `storage_backend`, `bucket`, `object_key`, `version_id`,
  `uri`
- integrity and policy: `sha256`, `size_bytes`, `content_type`,
  `redaction_policy`, `raw_capture`, `encryption_key_ref`
- retention: `retention_class`, `expires_at`
- unique `(uri)` and `(sha256, size_bytes, content_type)` where practical

`checks`

- parent to `attempt_id` and `runtime_invocation_id`
- store `command_hash`, `command_preview`, `shell`, `cwd_locator`,
  `env_metadata`
- link stdout/stderr artifacts separately
- keep exit code, signal, duration, started/completed timestamps

`git_provenance`

- keep `attempt_id`, `base_ref`, `remote`, `worktree_locator`,
  `merge_base_sha`, `patch_artifact_id`, `commit_message_hash`,
  `commit_message_preview`, changed-file and insert/delete counts
- keep PR URL/number as optional deployment artifact, not required for all runs

`runtime_errors`

- `id uuid primary key`
- `run_id`, `agent_name`, `attempt_id`, `session_id`, `invocation_id`
- `source text` (`spawnd`, `redis`, `postgres`, `artifact_store`, `otel`,
  `codex_sdk`, `codex_cli`, `claude_sdk`, `check`, `git`)
- `code text`
- `message_hash text`
- `message_preview text`
- `details_artifact_id uuid`
- `retryable boolean`
- `created_at timestamptz not null`

### Provider Event Envelope

`provider_events`

- `id uuid primary key`
- `run_id`, `agent_name`, `attempt_id`, `session_id`, `invocation_id`
- `provider text not null`
- `runtime text not null`
- `event_name text not null`
- `provider_event_id text`
- `provider_thread_id text`
- `provider_turn_id text`
- `provider_message_id text`
- `sequence bigint not null`
- `occurred_at timestamptz`
- `received_at timestamptz not null`
- `payload_schema text`
- `payload_version text`
- `payload_hash text`
- `payload_preview jsonb not null default '{}'`
- `payload_artifact_id uuid`
- unique `(session_id, sequence)`

This is the escape hatch for provider drift. New SDK event types can be
captured immediately without blocking on first-class columns, while the stable
facts are promoted into normalized tables as needed.

## Trace And Telemetry Shape

Keep OpenTelemetry as the external trace protocol and Postgres as the local
provenance ledger.

`trace_spans`

- current table is acceptable as the mirror
- future trace hardening can add `attempt_id`, `runtime_session_id`,
  `runtime_invocation_id`, `otel_resource`, `otel_scope`, `sampled`,
  `trace_flags`, and `dropped_attributes_count`
- record redacted attributes only
- never depend on successful OTLP export for internal status reconstruction

Recommended span hierarchy:

1. `spawnd.run`
2. `spawnd.worker.schedule`
3. `spawnd.agent.claim`
4. `spawnd.worktree.setup`
5. `spawnd.runtime.session`
6. `spawnd.runtime.invocation`
7. `spawnd.runtime.tool`
8. `spawnd.check`
9. `spawnd.artifact.upload`
10. `spawnd.git.provenance`
11. `spawnd.pr.create`

Telemetry failure policy:

- `degrade`: write `runtime_errors(source='otel')` and continue.
- `fail`: fail before unsafe execution if telemetry cannot initialize, and fail
  after execution if telemetry cannot flush a required terminal span.

## Coordination And Recovery

Postgres remains the source of truth; Redis remains an operational hint and
live-update plane.

Required production tables:

`worker_nodes`

- `worker_id text primary key`
- `hostname text`
- `version text`
- `started_at timestamptz`
- `heartbeat_at timestamptz`
- `capacity jsonb`
- `status text`

`queue_outbox`

- `id uuid primary key`
- `run_id`, `agent_name`, `event_type`
- `payload jsonb not null`
- `status text not null`
- `attempts int not null default 0`
- `next_attempt_at timestamptz`
- `created_at`, `published_at`

Use the outbox for Redis stream/pubsub writes and optional OTLP export jobs.
That avoids losing wakeups when a transaction commits but Redis publishing
fails.

Lease rules:

- Claim is one Postgres transaction: pending or queued agent becomes running,
  with `worker_id`, `lease_token`, `leased_until`, and an `agent_attempts` row;
  the same claim moves the parent run to `running`.
- Redis queue entries carry only run/agent/attempt hints.
- Workers renew both Postgres lease columns and Redis lease keys.
- Reconciler requeues from Postgres, not from Redis.
- Expired attempts move to queued or failed according to retry policy, with a
  new `agent_attempts` row for the next attempt, then refresh aggregate run
  status.
- Cancellation updates `runs` and affected `agents` first, then publishes Redis
  cancellation messages. It closes running attempts and clears worker
  ownership fields.
- Ready-agent publication records a `queue_outbox` row before writing Redis
  wakeups.

## Redaction And Artifact Policy

Default policy:

- Prompts: Postgres stores hash, size, and short redacted preview. Full prompt
  is an artifact only when product policy allows it.
- Environment: store key names, value hashes, sensitivity labels, and source;
  never store `.env` values. Freeform output redaction must catch sensitive
  `KEY=value` assignments, including deployed connection strings such as
  `SPAWND_DATABASE_URL` and `SPAWND_REDIS_URL`, while preserving non-secret
  assignments.
- Command output: redact then upload. Store content hash, size, line count, and
  artifact id.
- Paths: store repository-relative paths when possible; absolute paths are
  hashed or moved to `worktree_locator`.
- Thinking/reasoning: omit raw content by default. Store only presence, token
  counts, provider signature/hash, and redaction metadata unless explicit raw
  capture is enabled.
- Raw capture: requires `orchestration.artifacts.capture_raw=true`, a run-level
  audit event, and an artifact redaction policy value that is not `default`.

Every artifact upload should record:

- sha256 over stored bytes;
- pre-redaction content hash when allowed, otherwise null;
- byte size and line count;
- content type;
- redaction policy and redactor version;
- storage URI and object version;
- encryption/key reference;
- retention class and expiry.

## Indexing And Retention

Recommended indexes:

- `agents(run_id, status)`, `agents(worker_id, leased_until)`
- `agent_attempts(run_id, agent_name, attempt_number desc)`
- `runtime_sessions(attempt_id)`, `(provider, provider_session_id)`,
  `(provider, provider_thread_id)`
- `runtime_invocations(session_id, sequence)`, `(provider_turn_id)` where not null
- `runtime_messages(invocation_id, sequence)`
- `runtime_tool_calls(invocation_id, tool_name, status)`
- `token_usage(run_id, agent_name, created_at)`, `(model, scope, created_at)`
- `provider_events(session_id, sequence)`, `(run_id, received_at)`,
  `(provider, event_name, received_at)`
- `trace_spans(run_id, agent, started_at)`, `(trace_id, span_id)`
- `artifacts(run_id, kind, created_at)`, `(sha256, size_bytes)`
- `runtime_errors(run_id, created_at)`, `(source, code, created_at)`

Partition high-volume append tables by time or run shard:

- `events`
- `provider_events`
- `trace_spans`
- `runtime_content_blocks` if artifact-heavy indexing grows
- `token_usage`

Retention should be policy-driven:

- keep normalized provenance longer than raw artifacts;
- keep redacted artifacts long enough for PR review and incident response;
- expire raw-capture artifacts aggressively;
- keep hash-only rows after artifact deletion so provenance remains auditable.

## Runtime Ingestion Path

1. Keep `runs`, `agents`, `events`, `artifacts`, `checks`, `trace_spans`, and
   `git_provenance` as the deployed read model.
2. Continue writing `agent_attempts`, `runtime_sessions`,
   `runtime_invocations`, `token_usage`, `cost_usage`, `runtime_errors`, and
   `provider_events` from the observer boundary.
3. Populate `runtime_messages`, `runtime_content_blocks`, `runtime_items`,
   `runtime_tool_calls`, `runtime_tool_results`, `runtime_plans`,
   `runtime_file_changes`, `runtime_permissions`, `runtime_hooks`,
   `runtime_mcp_servers`, `runtime_mcp_tools`, `runtime_subtasks`, and
   `context_usage_snapshots` as each provider stream exposes stable facts.
4. Keep full provider payloads and large content in redacted artifacts. Promote
   only stable, query-worthy fields into first-class columns.
5. Stop writing provider details into generic `events.data` except for
   high-level run ledger events.
6. Keep aggregate columns on `agents` as rollups; derive them from normalized
   rows when the ingestion path is complete enough.

## Production Reliability Notes

- Add idempotency keys for run submission, artifact uploads, provider event
  ingestion, queue outbox rows, and PR creation.
- Add state-transition constraints so invalid status jumps cannot be committed
  accidentally.
- Record every worker binary/package version in `worker_nodes` and every SDK/CLI
  version in `runtime_sessions`.
- Add `schema_version` or `payload_version` to provider events because SDK wire
  shapes will drift.
- Treat provider event ingestion as at-least-once. Use `(session_id, sequence)`
  and provider ids for dedupe.
- Separate materialized state from the event ledger. Rebuild state from
  normalized rows plus events during incidents.
- Put live status views on Postgres read models and Redis pubsub. Redis pubsub
  may make those views fast, but it must never be required to reconstruct a run.
- Store cost and token usage at the narrowest reliable source scope, then roll
  up to agent/run asynchronously.
- Make redaction deterministic and versioned so artifact hashes can be trusted
  during incident review.
- Add dead-letter handling for artifact uploads, OTLP export, Redis publish, and
  PR creation. Dead letters should link back to `runtime_errors`.
- Add tenant/project columns before external deployment if more than one owner
  or repository can share the same database.
- Use database roles or row-level security if external users will query runs
  directly.
