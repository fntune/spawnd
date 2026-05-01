# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**spawnd.dev** — Multi-agent orchestration framework.

One scheduler, one SQLite state store, pluggable vendor runtimes. Executes agents in parallel git worktrees with DAG dependency resolution, retry logic, manager/worker hierarchies, and branch merging. Two frontends (CLI + Python API) share the same machinery.

## Commands

```bash
# Install
pip install -e .                  # core
pip install -e ".[sdk]"           # + Claude Agent SDK
pip install -e ".[openai]"        # + OpenAI Agents SDK (gpt-5 etc.)
pip install -e ".[dev]"           # both SDKs + pytest

# Type + tests
pyright spawnd/
pytest tests/                     # unit suite; tests/sdklive/ is excluded
pytest tests/test_scheduler.py -xvs                          # one file
pytest tests/test_scheduler.py::test_scheduler_init -xvs     # one test
# tests/sdklive/ are manual integration scripts (real API calls).
# Run directly with `python tests/sdklive/<file>.py`, not via pytest.

# CLI (10 commands)
spawnd run -f plan.yaml
spawnd run -p "auth: Impl auth"
spawnd run -p "a: s1" -p "b: s2" --sequential
spawnd run --run-id <id> -p "..."
spawnd run --resume --run-id <id>
spawnd run -p "t: true" --mock     # no SDK calls

spawnd resume <run_id>
spawnd status [run_id] [--json]
spawnd logs <run_id> -a <agent> [-f]
spawnd logs <run_id> --all
spawnd merge <run_id> [--dry-run]
spawnd cancel <run_id>
spawnd dashboard <run_id>
spawnd clean [run_id] [--all]
spawnd db <run_id> [query]
spawnd roles [name]
```

## Python API

The same scheduler is a library — runs started from Python land in the same `.spawnd/runs/<run_id>/` and are inspectable via the CLI commands above.

```python
import asyncio
from spawnd import run, pipeline, handoff, agent

# Full DAG run (optionally mixing runtimes per agent)
asyncio.run(run([
    agent("impl",   "step 1"),
    agent("review", "step 2", runtime="openai", depends_on=["impl"]),
], name="my-run"))

# Sequential sugar (auto-chains depends_on)
asyncio.run(pipeline([
    agent("gen", "generate"),
    agent("rev", "review", use_role="reviewer"),
]))

# Two-step handoff
asyncio.run(handoff(agent("impl", "build"), agent("audit", "review")))
```

`agent()` is a Python builder for `AgentSpec`; `run()` also accepts a full `PlanSpec` directly. All features (retries, circuit breaker, manager spawn, blocking coord, resume) work identically from both frontends.

## Architecture

```
spawnd/
├── cli.py                   # Click CLI — 10 commands
├── api.py                   # Python API — run / pipeline / handoff / agent
├── roles.py                 # 7 built-in role templates
├── core/
│   ├── budget.py            # Token→USD price table (OpenAI runtime)
│   └── deps.py              # DependencyGraph, topological sort
├── io/                      # parser.py, plan_builder.py, validation.py
├── models/                  # specs.py (AgentSpec/PlanSpec/Defaults), state.py
├── runtime/
│   ├── scheduler.py         # Poll loop, retry, circuit breaker, stuck detection
│   ├── executor.py          # AgentConfig + spawn_worker/spawn_manager dispatch
│   ├── task_registry.py     # run_id → asyncio.Task registry (for cancel)
│   └── executors/
│       ├── base.py          # Executor ABC, EXECUTOR_REGISTRY, get_executor
│       ├── claude.py        # ClaudeExecutor (runtime="claude")
│       └── openai.py        # OpenAIExecutor (runtime="openai", optional [openai] extra)
├── storage/                 # db.py (SQLite WAL), logs.py, paths.py
├── tools/
│   ├── worker.py            # mark_complete, request_clarification, report_progress, report_blocker
│   ├── manager.py           # spawn_worker, cancel_worker, mark_plan_complete, ...
│   ├── toolset.py           # Toolset dataclass + worker_toolset() / manager_toolset()
│   ├── factory.py           # @tool wrappers for Claude SDK MCP server
│   ├── factory_openai.py    # @function_tool wrappers for OpenAI coord ops
│   └── openai_code.py       # @function_tool wrappers for Read/Write/Edit/Bash/Glob/Grep
└── gitops/                  # worktrees.py, merge.py
```

## Runtimes

Pluggable vendor executors, selected per-agent via `runtime:` (defaults to `claude`). Manager vs worker is a `Toolset` difference, not a runtime difference — `Executor.run(config, toolset) -> dict` is the only method each runtime implements.

| Runtime | Package | Default model | Cost source |
|---|---|---|---|
| `claude` | `claude-agent-sdk` (`[sdk]`) | `sonnet` | SDK-reported USD (`cost_source="sdk"`) |
| `openai` | `openai-agents` (`[openai]`) | `gpt-5` | Estimated from tokens × price table (`cost_source="estimated"`) |

Adapters self-register at `spawnd.runtime.executors` import time. `get_executor(runtime)` raises `ExecutorNotFound` for unknown names. OpenAI import is guarded, so spawnd works without the `[openai]` extra installed.

Mixed-runtime plans work — dependencies and manager→worker spawn flow across vendors. Manager-spawned children inherit the parent's runtime + cost_source.

## Failure Handling

- `on_failure: continue` (default) — continue with other agents
- `on_failure: stop` — cancel all on first failure
- `on_failure: retry` — retry up to `retry_count`, injecting last error into the retry prompt
- `cost_exceeded` is a proper terminal state — never retried by `on_failure: retry`
- Cascade failures — agents with failed deps are auto-marked failed
- Circuit breaker — trip on N failures (`cancel_all` / `pause` / `notify_only`)
- Stuck detection — keyed on newest event id, not a rolling count

## Coordination Tools

Plain-`str` returns (no Claude content-block shape); Claude SDK wrapping happens only at `spawnd/tools/factory.py`. OpenAI `@function_tool` wrappers in `factory_openai.py` use the same functions directly.

- Worker (`spawnd/tools/worker.py`): `mark_complete`, `request_clarification`, `report_progress`, `report_blocker`
- Manager (`spawnd/tools/manager.py`): `spawn_worker`, `respond_to_clarification`, `cancel_worker`, `get_worker_status`, `get_pending_clarifications`, `mark_plan_complete`

`request_clarification` and `report_blocker` block the worker until the manager responds. `parent` / `tree_path` identifiers flow through the closure, not `os.environ` — two concurrent agents in the same process don't clobber each other.

## Roles

`architect`, `implementer`, `tester`, `reviewer`, `debugger`, `refactorer`, `documenter`. Apply via `use_role: <name>` in YAML or `use_role="..."` in the Python API.

## Plan Spec Format

```yaml
name: my-plan
defaults:
  check: "pytest tests/"
  on_failure: retry
  retry_count: 3
  model: sonnet
  runtime: claude               # or openai
orchestration:
  circuit_breaker: {threshold: 3, action: cancel_all}
agents:
  - name: auth
    prompt: "Implement authentication"
    use_role: implementer
  - name: review
    prompt: "Review the auth implementation"
    use_role: reviewer
    runtime: openai              # per-agent override
    depends_on: [auth]
```

## File Layout

```
.spawnd/runs/{run_id}/
├── spawnd.db                     # SQLite WAL; idempotent migration on init_db
├── worktrees/{agent}/           # Git worktree per agent
└── logs/{agent}.log             # Per-agent log file (tail -f compatible)
```

## SQLite Schema (agents table)

Key columns: `name`, `status`, `type`, `runtime`, `parent`, `vendor_session_id`, `cost_usd`, `cost_source`, `retry_count`, `retry_attempt`, `env`, `max_subagents`, `depends_on`. `_migrate_agents()` at `spawnd/storage/db.py` runs on every `init_db` — adds missing columns + renames `session_id` → `vendor_session_id` on legacy DBs.

## Dependencies

- `pydantic>=2.0` — spec + state models
- `click>=8.0` — CLI
- `pyyaml>=6.0` — plan parsing
- `claude-agent-sdk>=0.1.19` (opt `[sdk]`)
- `openai-agents>=0.6`, `openai>=1.50` (opt `[openai]`)

## Style

- SQLite WAL mode for concurrent agent access
- Logs are files (append-only, `tail -f` compatible)
- Coord tools return plain `str`; SDK-specific wrapping at boundary only
- Never read `os.environ` from coord tools — thread context via closures
- Idempotent schema migration on every DB open

## Code Patterns

### Database Access

```python
from spawnd.storage.db import get_db, get_agents

with get_db(run_id) as db:
    agents = get_agents(db, run_id)
```

### Path Helpers (`spawnd.storage.paths`)

`get_run_dir(run_id)`, `get_db_path(run_id)`, `get_logs_dir(run_id)`, `get_worktrees_dir(run_id)`, `ensure_log_file(run_id, agent_name)`.

### Adding a Runtime

1. Subclass `spawnd.runtime.executors.base.Executor`, set `runtime = "my-vendor"`, implement `async run(config, toolset) -> dict`.
2. Call `register(MyExecutor())` at module scope.
3. Import the module from `spawnd/runtime/executors/__init__.py` (guarded with `try/except ImportError` if the SDK is optional).
4. Agents using `runtime: my-vendor` now dispatch through your executor. Cost should be recorded with an appropriate `cost_source`.
