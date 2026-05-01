# spawnd multi-runtime refactor

> **2026-04-19 update — partially superseded.**
>
> Phase 3 ("Live mode" as a reduced-feature runner with in-memory state, no DAG, no manager spawn) is **cancelled**. It has been replaced by a unified Python API (`spawnd.api.run / pipeline / handoff / agent`) that always uses the same scheduler, worktrees, and SQLite state as the CLI. See `.claude/plans/lets-work-on-bringing-tender-zebra.md` for the cancellation rationale and the replacement design.
>
> **Phases 1, 2, and 4 remain valid** and should be reframed as "one runner with pluggable vendor executors" rather than "batch mode internals." When implementing them, build on top of `spawnd/api.py` (introduced in the parity work) so both CLI and Python callers get the new runtimes for free.

## Context

`~/dev/spawnd` is a Python multi-agent orchestration framework. Today it runs on the Claude Agent SDK only. The user wants spawnd to also run OpenAI Agents SDK workers as a first-class runtime, and to add a "live mode" for single-run agent handoffs (no YAML, no SQLite, no worktree ceremony).

Why this matters now: the user's `.claude/skills/` directory already encodes cross-vendor workflows (`debt`, `audit-fix-loop`, `swarm-audit`) that orchestrate Claude + Codex (GPT-5) interactively inside Claude Code sessions. There is no way to run those patterns as batch spawnd plans today. spawnd is the right home — the orchestration graph, worktree isolation, SQLite resume, and circuit breaker are already there; the only thing missing is vendor neutrality in the execution layer.

Exploration confirmed the coupling surface is narrower than expected: `runtime/executor.py`, `tools/factory.py`, ~4 lines in `models/specs.py`, `scheduler.py:_spawn_agent()`, and the `agents` table schema. Everything above the executor (CLI, parser, deps graph, scheduler poll loop, gitops, merge, db helpers) is already vendor-neutral. `tools/worker.py` and `tools/manager.py` have no Claude SDK imports — only their return shape happens to match Claude's content-block format.

A stress-test pass surfaced seven meaningful corrections to my initial plan — folded in below. The single biggest change: the executor ABC should be `run(config, toolset, observer)` with one method, not the `run_worker`/`run_manager` pair I originally proposed. Manager vs worker becomes toolset construction (a scheduler concern), not a per-vendor concern.

## Target architecture

```
spawnd/
  runtime/
    executors/
      __init__.py       # registry: {runtime: Executor}
      base.py           # Executor ABC + Observer protocol + StepResult
      claude.py         # ClaudeExecutor (current logic, reshaped)
      openai.py         # OpenAIExecutor (Phase 2)
    executor.py         # thin dispatcher — preserves public fn signatures
    scheduler.py        # unchanged except _spawn_agent picks runtime
    live.py             # Phase 3: pipeline/handoff primitives
  tools/
    worker.py           # vendor-neutral, returns str/ToolResult (refactored)
    manager.py          # vendor-neutral, returns str/ToolResult (refactored)
    factory.py          # Claude @tool wrapping (existing, signatures change)
    factory_openai.py   # Phase 2: @function_tool wrapping
    openai_code.py      # Phase 2: read/edit/shell/grep/glob as @function_tool
    toolset.py          # Phase 1: Toolset dataclass (coord, code, write_allowed)
  models/
    specs.py            # + runtime, + output_schema, loosen model
    state.py            # unchanged
  storage/
    db.py               # schema migration: + runtime, + cost_source, rename session_id
  roles.py              # + tools field, + read_only
  core/
    errors.py           # Phase 1: SpawndExecutorError
    budget.py           # Phase 4: shared budget + cost table
    tracing.py          # Phase 4: OTel unification
  bridge/
    as_tool.py          # Phase 3: agent-as-tool wrappers
    mcp.py              # Phase 4 (deferred): shared MCP tool pool
  cli.py                # Phase 4: spawnd report <run_id>
  examples/
    cross_check.py      # Phase 3: generator-reviewer hello world
    debt.py             # Phase 3: debt skill port
```

## Phase 1 — Executor ABC + Observer + Toolset refactor

**Goal:** One internal reshape that unblocks everything else. No new deps. No user-visible behavior change. All tests green.

### Changes

1. **`spawnd/runtime/executors/base.py` (new)** — define:
   - `Observer` protocol: `on_start(config)`, `on_iteration(iteration)`, `on_event(event_type, data)`, `on_cost(usd)`, `on_complete(status, error=None)`. All no-ops by default.
   - `DBObserver(run_id)` — writes to SQLite via existing `storage/db.py` helpers (`update_agent_status`, `insert_event`, `update_agent_iteration`, `update_agent_cost`). This is the batch-mode observer.
   - `NullObserver` — in-memory only. Used by live mode (Phase 3) and tests.
   - `StepResult` dataclass: `final_text: str`, `cost_usd: float`, `vendor_session_id: str | None`, `structured_output: Any = None`, `files_modified: list[str] | None = None`, `error: str | None = None`, `status: Literal["completed","failed","timeout","cancelled","cost_exceeded"]`.
   - `Executor` ABC:
     ```python
     class Executor(ABC):
         runtime: ClassVar[str]
         @abstractmethod
         async def run(
             self,
             config: AgentConfig,
             toolset: Toolset,
             observer: Observer,
         ) -> StepResult: ...
     ```
   - `EXECUTOR_REGISTRY: dict[str, Executor]` and `register(executor)` / `get_executor(runtime)`.

2. **`spawnd/tools/toolset.py` (new)** — `Toolset` dataclass:
   ```python
   @dataclass
   class Toolset:
       coord: list[str]          # e.g. ["mark_complete", "report_progress"]
       code: list[str]           # e.g. ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
       write_allowed: bool = True
       system_prompt: str = ""
       role: str | None = None
   ```
   Construction lives in `scheduler.py` (batch) or in live-mode helpers (Phase 3). Manager vs worker is a toolset difference, not an executor difference.

3. **`spawnd/tools/worker.py` + `spawnd/tools/manager.py` (refactor return type)** — change every coordination function to return `str` (or a small `ToolResult(text: str, success: bool = True)` dataclass — pick the simpler path, which is plain `str`). The Claude content-block wrapping happens *only* in `tools/factory.py` at the boundary. Rationale from the critique: OpenAI `@function_tool` serializes dict returns to JSON and the LLM has to unwrap `content[0].text` on every call — token waste and confusion. Keeping the content-block shape in worker/manager also locks us into Claude semantics. This is the most important change in Phase 1.

4. **`spawnd/tools/worker.py:89` + `manager.py` env leak fix** — `tools/worker.py` currently reads `SPAWND_PARENT_AGENT` / `SPAWND_TREE_PATH` from `os.environ`. Today this works because in-process MCP runs in the parent Python process; once OpenAI agents run concurrently in the same process, `os.environ` will leak between them. Thread `parent` and `tree_path` through the tool closure in `factory.py` (and `factory_openai.py` in Phase 2) instead. Latent bug fix that unblocks concurrent multi-runtime execution.

5. **`spawnd/runtime/executors/claude.py` (new)** — move existing `run_worker`/`run_manager` bodies here, collapse into a single `ClaudeExecutor.run(config, toolset, observer)`. Inside: build `ClaudeAgentOptions` from `toolset.code + ["mcp__spawnd__" + t for t in toolset.coord]`, set `permission_mode="plan"` when `toolset.write_allowed=False`, pass `toolset.system_prompt`. Emit observer callbacks on each `AssistantMessage` / `ResultMessage`. Return `StepResult` instead of a loose dict.

6. **`spawnd/runtime/executor.py` (thin dispatcher)** — keep the public functions `spawn_worker(config, use_mock)` and `spawn_manager(config)` so `scheduler.py` doesn't change. Internally they now:
   - Build a `Toolset` for the worker/manager shape
   - Look up `get_executor(config.runtime)` from the registry
   - Create a `DBObserver(config.run_id)`
   - Call `executor.run(config, toolset, observer)`
   - Wrap in `asyncio.create_task` (preserving the current scheduler contract)

7. **`spawnd/models/specs.py`** — add `runtime: Literal["claude","openai"] = "claude"` to both `Defaults` and `AgentSpec`. Loosen `model` from `Literal["sonnet","opus","haiku"]` to `str | None`. Add `output_schema: dict | None = None` to `AgentSpec` (maps to OpenAI `output_type` in Phase 2; documented as ignored by Claude executor for now). Add `tools: list[str] | None = None` to `AgentSpec` (role override).

8. **`spawnd/models/specs.py` — default_model_for helper**:
   ```python
   def default_model_for(runtime: str) -> str:
       return {"claude": "sonnet", "openai": "gpt-5"}.get(runtime, "sonnet")
   ```
   Used by `tools/manager.py:spawn_worker` (currently hardcodes `"sonnet"`) and `scheduler.py:216` (same).

9. **`spawnd/roles.py`** — add `tools: list[str] | None = None` and `read_only: bool = False` fields to `RoleTemplate`. Set `reviewer` role's `read_only=True` (maps to `["Read","Grep","Glob"]` code tools, `write_allowed=False` in Toolset). Defer filling in other roles until Phase 2.

10. **`spawnd/storage/db.py` schema migration** — add three columns to `agents` table:
    - `runtime TEXT NOT NULL DEFAULT 'claude'`
    - `cost_source TEXT NOT NULL DEFAULT 'sdk'` — `"sdk"` for Claude (authoritative), `"estimated"` for OpenAI (computed from tokens in Phase 2). Avoids a later bug report.
    - Rename `session_id` → `vendor_session_id`. **Don't migrate twice.** Update `insert_agent`, `get_agent`, and callers.
    
    Use an idempotent ALTER + `PRAGMA table_info` check so existing `.spawnd/runs/*/spawnd.db` (and legacy `swarm.db`) files keep working.

11. **`spawnd/core/errors.py` (new)** — `SpawndExecutorError(message: str, retryable: bool = False, cost_so_far: float = 0.0)`. Normalize at the executor boundary so scheduler retry logic doesn't special-case vendor exception types.

12. **`spawnd/runtime/scheduler.py:_spawn_agent()`** — read `runtime` from agent row, pass into `AgentConfig`. Use `default_model_for(runtime)` instead of hardcoded `"sonnet"` on line 216.

13. **`spawnd/runtime/executor.py:AgentConfig`** — add `runtime: str = "claude"` field.

### Critical files to read before editing

- `/Users/sour4bh/dev/spawnd/runtime/executor.py` — source of `run_worker`/`run_manager` duplication that collapses into one method
- `/Users/sour4bh/dev/spawnd/runtime/scheduler.py` — lines 162–227 (`_spawn_agent`) and line 216 (hardcoded model default)
- `/Users/sour4bh/dev/spawnd/tools/worker.py` — return shape refactor + line 89 env leak
- `/Users/sour4bh/dev/spawnd/tools/manager.py` — return shape refactor + model default on line 25
- `/Users/sour4bh/dev/spawnd/tools/factory.py` — wrapping boundary for Claude content-block format
- `/Users/sour4bh/dev/spawnd/storage/db.py` — `insert_agent`, `update_agent_*` helpers; schema around lines 27–55
- `/Users/sour4bh/dev/spawnd/models/specs.py` — lines 22, 96 for model Literal; field additions
- `/Users/sour4bh/dev/spawnd/roles.py` — `RoleTemplate` around lines 6–14
- `/Users/sour4bh/dev/spawnd/tests/test_executor.py` — line 151 default assertion (`config.model == "sonnet"`)
- `/Users/sour4bh/dev/spawnd/tests/test_roles.py` — lines 27, 42 role-model assertions

### Reused utilities

- `storage/db.py` helpers: `insert_event`, `update_agent_status`, `update_agent_iteration`, `update_agent_cost`, `insert_agent`, `get_agent` — all reused by `DBObserver`, no rewrites
- `gitops/worktrees.py` — untouched in Phase 1; reused by Phase 3 live mode
- `core/deps.py` — untouched; vendor-neutral
- `io/parser.py`, `io/validation.py`, `io/plan_builder.py` — untouched; Pydantic auto-accepts new `runtime`/`output_schema`/`tools` fields
- `tools/worker.py` and `tools/manager.py` function bodies — **logic** unchanged, only return types refactored

### Verification

1. `pytest tests/` — all existing tests pass (the two brittle assertions at `test_executor.py:151` and `test_roles.py:27,42` continue to hold because defaults don't change)
2. New test `tests/test_executor_registry.py`: construct `AgentConfig(runtime="claude", ...)`, call dispatch, assert the `ClaudeExecutor` instance was returned. Also test `runtime="openai"` raises `KeyError` (no OpenAI executor registered yet) — this is the contract gate for Phase 2.
3. New test `tests/test_toolset.py`: assert `Toolset(write_allowed=False)` produces a code-tool list without `Write`/`Edit`/`Bash`, and that the Claude executor would set `permission_mode="plan"` for it (test via a mock observer, don't spawn a real client).
4. Run existing `tests/sdklive/` smoke test against a real Claude API to confirm end-to-end batch execution still works. This is the gate before calling Phase 1 done.
5. Run `spawnd run -f tests/fixtures/simple-plan.yaml --mock` to confirm the CLI path still works.

### Exit criteria

- All existing unit tests green
- New registry + toolset tests green
- sdklive smoke test green with real Claude API
- `--mock` CLI path works end-to-end
- No new deps added
- `git diff --stat` touches only the files listed in "Changes" above

## Phase 2 — OpenAI executor + code tool pack

**Goal:** Second runtime shipped. Batch plans can mix `runtime: claude` and `runtime: openai` agents with dependencies between them.

### Changes (summary, details deferred to Phase 2 planning session)

- New optional extra: `pip install -e ".[openai]"` → adds `openai-agents` + `openai`
- `runtime/executors/openai.py` — wraps `Agent`/`Runner`. Budget enforcement: poll `RunResult.usage` after each turn, abort when exceeded. Cost = token count × price table (from `core/budget.py` — promoted in Phase 4, inlined here). `cost_source="estimated"`.
- `tools/factory_openai.py` — wraps the same `tools/worker.py` / `tools/manager.py` functions with `@function_tool` instead of `@tool`. Thread `parent` / `tree_path` through closures (Phase 1 fix makes this clean).
- `tools/openai_code.py` — `@function_tool` parity with Claude's built-ins: `read_file`, `edit_file`, `run_shell` (with sandbox-aware timeout), `grep`, `glob`. Respects `toolset.write_allowed=False` by skipping write/shell tools at construction time.
- `roles.py` — fill in `read_only`/`tools` for the other 6 roles (explorer = read-only, implementer = full, etc.)
- `core/errors.py` — catch `openai_agents.AgentError` / Runner exceptions, normalize to `SpawndExecutorError`
- New `tests/sdklive/test_sdk_openai.py` — smoke test with real OpenAI key
- New `tests/test_mixed_runtime_plan.py` — a plan with one claude agent and one openai agent, `depends_on: [claude_one]`, `--mock` for CI + live under `sdklive/`
- Cancellation test: spawn OpenAI worker, cancel mid-flight, verify no leaked tasks

### Phase 2 exit criteria

- `examples/plans/debt.yaml`, `examples/plans/review-3x.yaml`, `examples/plans/evolve.yaml` (the three mixed-vendor plans identified from user skills) all run to completion in mock + live modes

## Phase 3 — Live mode

**Goal:** `async def pipeline(steps)` and `async def handoff(a, b)` primitives that execute without YAML, scheduler, or SQLite ceremony — but *do* use worktrees (reused from `gitops/worktrees.py`).

### Changes (summary)

- `runtime/live.py`:
  ```python
  async def pipeline(
      steps: list[AgentConfig],
      workspace: Literal["worktree", "cwd", "tempdir"] = "worktree",
      keep: bool = False,
  ) -> list[StepResult]: ...
  async def handoff(from_step: AgentConfig, to_step: AgentConfig, **kwargs) -> StepResult: ...
  ```
  - `workspace="worktree"` (default) allocates under `.spawnd/live/<uuid>/` via existing `gitops/worktrees.py`; cleaned up unless `keep=True`
  - `workspace="cwd"` runs in `Path.cwd()` — fast, no isolation
  - `workspace="tempdir"` uses `tempfile.TemporaryDirectory()` — ephemeral, no git history
  - Live mode uses `NullObserver` — no SQLite
  - Coordination tools that depend on SQLite (`request_clarification`, `report_blocker`) are gated out of live-mode toolsets. They're batch-only. Document this and raise if a live-mode agent tries to use them (toolset construction raises `ValueError("tool X requires batch mode")`).
  - Managers (agents that spawn workers) are forbidden in live mode for Phase 3. Raise `NotImplementedError` + point to batch mode. Revisit in Phase 4+ with a live micro-scheduler.
- `bridge/as_tool.py`:
  ```python
  def as_claude_tool(agent: AgentConfig) -> Any: ...        # @tool
  def as_openai_tool(agent: AgentConfig) -> Any: ...        # @function_tool
  ```
  Implementation: each wrapper spawns a `pipeline([agent])` call under the covers and returns `StepResult.final_text` (or `structured_output` if schema was set).
- `examples/cross_check.py` — generator (Claude, runtime="claude") → reviewer (OpenAI, runtime="openai", `output_schema=ReviewFindings`). The hello world.
- `examples/debt.py` — port the `debt` skill shape:
  1. `git diff --name-only HEAD~1` to get changed files
  2. Parallel Claude audit workers, one per file batch (read-only toolset)
  3. OpenAI cross-reference with `output_schema=TechnicalDebtReport` that validates findings + adds new ones
  4. Claude fixers on high+medium severity only
  5. Verify with `pytest` + `ruff check`
  Runs via `runtime/live.py` with `workspace="worktree"`.

### Phase 3 exit criteria

- `python examples/cross_check.py` produces a reviewed patch
- `python examples/debt.py` on a seeded repo with a deliberate bug produces a fix
- No SQLite files created during live runs
- `bridge/as_tool.py` tests: wrap an OpenAI agent as a Claude `@tool`, call it from inside a Claude executor, verify StepResult comes back

## Phase 4 — Unification

**Goal:** Telemetry, reporting, and the MCP bridge. These are nice-to-haves that wait until Phase 3 proves the abstractions.

### Changes (summary)

- `core/tracing.py` — shared `run_id` → OTel setup. Install OTel trace processor into OpenAI Agents SDK (`add_trace_processor`). Enable Claude Code OTel via `OTEL_*` env vars in `ClaudeExecutor.run`. Both vendors' spans land in one backend.
- `core/budget.py` — promote the price table + budget enforcement out of `executors/openai.py`. Shared across executors.
- `cli.py` — new `spawnd report <run_id>` command. Emits an optimization report format (per-agent utilization, bottlenecks, suggested next-run config). spawnd already logs events to SQLite; this is a query + formatter.
- `bridge/mcp.py` — shared function registry → Claude in-process MCP server (via `create_sdk_mcp_server`) + OpenAI stdio MCP server (via `MCPServerStdio`). Promote only when ≥3 shared tools exist to justify the abstraction.

## Out of scope (flagged for later)

- **Skill → plan YAML converter**: the user's skills are already structured multi-phase workflows. A future tool could ingest `SKILL.md` files and emit `PlanSpec` YAML. Makes spawnd the runtime for the skill library. Phase 5+.
- **Durable pause/resume with external hooks** (inspired by `workflow` skill's `createHook`): evolve `request_clarification` into a durable pause-and-resume primitive. Phase 5+.
- **Manager agents in live mode**: forbidden in Phase 3, revisit with a live micro-scheduler.

## Naming

Keep the **spawnd** / **spawnd.dev** naming; the CLI entrypoint is `spawnd`. README tagline can broaden to "multi-agent orchestration for coding agents (Claude + OpenAI)" when messaging needs it.

## Execution order

Work branch: `feat/multi-runtime`. One PR per phase. Phase 1 is the only one where the internal plumbing changes in a way that requires care; Phases 2–4 are additive.

Start with Phase 1. Phase 1 includes the tool-return-type refactor (critical — don't defer). Exit criteria are strict: all existing tests green + new registry/toolset tests + sdklive smoke test + `--mock` CLI path. Only then start Phase 2.
