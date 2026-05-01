# AGENTS.md

## Project Overview

**spawnd.dev** — Multi-agent orchestration for Claude Code.

One scheduler, one SQLite-backed state store, two frontends:

- **CLI** (`spawnd run -f plan.yaml` or `spawnd run -p "name: prompt"`) — declarative YAML plans.
- **Python API** (`from spawnd import run, pipeline, handoff, agent`) — the same scheduler, invoked as a library.

Both frontends share the same machinery: DAG dependencies, parallel execution in isolated git worktrees, retry with error-context injection, circuit breaker, manager-spawn hierarchies, blocking worker↔manager coordination, and resume. Runs started from either frontend land in the same `.spawnd/runs/<run_id>/` directory and are inspectable via the same CLI commands.

## Commands

```bash
# Development
pip install -e .
pip install -e ".[sdk]"        # add Claude Agent SDK
pip install -e ".[dev]"        # SDK + pytest + pytest-asyncio
spawnd --help

# Testing
pytest tests/                              # unit tests
pytest tests/test_scheduler.py -xvs        # one file
pytest tests/test_scheduler.py::test_scheduler_init -xvs
# tests/sdklive/ are manual integration scripts (real API calls) —
# run with `python tests/sdklive/test_sdk_live.py`, not pytest.

# Type checking
pyright spawnd/

# CLI
spawnd run -f plan.yaml                     # execute plan spec
spawnd run -p "auth: Impl auth"             # inline single agent
spawnd run -p "a: step1" -p "b: step2" --sequential
spawnd run --run-id <id> -p "..."           # explicit run ID
spawnd run --resume --run-id <id>           # resume existing run
spawnd run -p "test: noop" --mock           # dev-only: skip SDK calls

spawnd resume <run_id>                      # resume alias
spawnd status [run_id] [--json]             # view status (latest if no id)
spawnd logs <run_id> -a <agent>             # view agent logs
spawnd logs <run_id> --all                  # view all logs
spawnd merge <run_id> [--dry-run]           # merge completed branches
spawnd cancel <run_id>                      # cancel running agents
spawnd dashboard <run_id>                   # live status view
spawnd clean [run_id] [--all]               # clean up artifacts
spawnd db [run_id] [query]                  # query SQLite database
spawnd roles [name]                         # list/view built-in roles
```

## Python API

The library exposes the same scheduler the CLI drives. No parallel universe — a `run()` call produces a run indistinguishable from `spawnd run -f plan.yaml`.

```python
import asyncio
from spawnd import run, pipeline, handoff, agent

# Single-agent run
await run([agent("auth", "Implement auth", check="pytest")], name="auth-run")

# DAG with dependencies
await run([
    agent("schema", "Design DB schema"),
    agent("api",    "Implement API",   depends_on=["schema"]),
    agent("tests",  "Write tests",     depends_on=["api"], use_role="tester"),
])

# Sequential sugar: pipeline auto-chains depends_on by list order
await pipeline([
    agent("generate", "Write a fibonacci function"),
    agent("review",   "Review the function above", use_role="reviewer"),
])

# Two-step handoff sugar
await handoff(
    agent("impl",  "Build the cache layer"),
    agent("audit", "Audit the implementation", use_role="reviewer"),
)
```

The `agent()` builder is a Python-friendly constructor for `AgentSpec` — omitted kwargs fall through to plan/role/global defaults. `run()` also accepts a full `PlanSpec` directly.

## Architecture

```
spawnd/
├── cli.py              # Click CLI — 10 commands
├── api.py              # Python API — run / pipeline / handoff / agent
├── core/
│   └── deps.py         # DependencyGraph, topological sort
├── io/
│   ├── parser.py       # YAML → PlanSpec, run-id generation
│   ├── plan_builder.py # Inline plan construction, shared-context loading
│   └── validation.py   # Plan validation (circular deps, unknown deps)
├── models/
│   ├── specs.py        # AgentSpec, PlanSpec, Defaults, CostBudget, ...
│   └── state.py        # AgentState, Event, Response
├── runtime/
│   ├── executor.py     # AgentConfig, run_worker, run_manager, run_worker_mock
│   ├── scheduler.py    # Scheduler poll loop, dispatch, retry, circuit breaker
│   └── task_registry.py# run_id → asyncio.Task registry (for cancel)
├── storage/
│   ├── db.py           # SQLite schema + helpers (WAL mode)
│   ├── logs.py         # Per-agent log files, tail -f support
│   └── paths.py        # .spawnd/runs/<run_id>/ path helpers
├── tools/
│   ├── factory.py      # @tool wrapping for Claude SDK MCP server
│   ├── worker.py       # mark_complete, request_clarification, report_progress, report_blocker
│   └── manager.py      # spawn_worker, respond_to_clarification, cancel_worker, get_worker_status, get_pending_clarifications, mark_plan_complete
├── gitops/
│   ├── worktrees.py    # create/cleanup worktrees, dep context merging
│   └── merge.py        # Branch consolidation
└── roles.py            # 7 built-in role templates (architect, implementer, tester, reviewer, debugger, refactorer, documenter)

tests/
├── test_*.py           # pytest units
└── sdklive/            # manual integration scripts (real Claude API)
```

`WARP.md` mirrors this file for the Warp terminal's agent — keep the two in sync when editing project instructions.

## Key Features

### Execution
- **Parallel agents** in isolated git worktrees
- **DAG dependencies** with topological ordering
- **Sequential sugar** via `--sequential` CLI flag or `pipeline()` Python helper
- **Resume** via `--resume --run-id` or `spawnd resume` — completed agents stay completed

### Failure handling
- **on_failure: continue** — default; continue with other agents
- **on_failure: stop** — cancel all on first failure
- **on_failure: retry** — retry up to `retry_count` with error context injected into the retry prompt
- **Cascade failures** — agents with failed deps are marked failed automatically
- **Circuit breaker** — trip after N failures (`cancel_all`, `pause`, `notify_only`)
- **Stuck detection** — flag runs with no event activity over N poll iterations

### Coordination
Worker tools (via in-process MCP): `mark_complete`, `request_clarification`, `report_progress`, `report_blocker`. `request_clarification` and `report_blocker` block the worker until the manager responds.

Manager tools: `spawn_worker`, `respond_to_clarification`, `cancel_worker`, `get_worker_status`, `get_pending_clarifications`, `mark_plan_complete`.

### Roles
Seven built-in templates: `architect`, `implementer`, `tester`, `reviewer`, `debugger`, `refactorer`, `documenter`. Apply via `use_role: <name>` in YAML or `use_role="..."` in the Python API.

## Plan Spec Format

```yaml
name: my-plan
defaults:
  check: "pytest tests/"
  on_failure: retry
  retry_count: 3
  model: sonnet
orchestration:
  circuit_breaker:
    threshold: 3
    action: cancel_all
agents:
  - name: auth
    prompt: "Implement authentication"
    use_role: implementer
  - name: tests
    prompt: "Write tests for auth"
    use_role: tester
    depends_on: [auth]
  - name: coordinator
    type: manager
    prompt: "Orchestrate follow-up work"
    depends_on: [tests]
    manager:
      max_subagents: 3
```

## File Layout

```
.spawnd/
└── runs/{run_id}/
    ├── spawnd.db                # SQLite state (WAL mode)
    ├── worktrees/{agent}/      # Git worktrees (one per agent)
    └── logs/{agent}.log        # Per-agent log files (append-only)
```

## Dependencies

- `pydantic>=2.0` — YAML boundary + spec models
- `click>=8.0` — CLI
- `pyyaml>=6.0` — plan spec parsing
- `claude-agent-sdk>=0.1.19` (optional `[sdk]` extra) — Claude runtime

## Style

- Follow global AGENTS.md conventions
- SQLite WAL mode for concurrent agent access
- Logs stay as files (for `tail -f` compatibility)

## Code Patterns

### Database Access

```python
from spawnd.storage.db import get_db, get_agents, get_plan

with get_db(run_id) as db:
    for agent in get_agents(db, run_id):
        print(agent["name"], agent["status"])
```

Use `open_db()` for short-lived read-only access or `get_db()` as a context manager. Always call helpers from `spawnd.storage.db` rather than raw SQL.

### Path Helpers

Centralized in `spawnd/storage/paths.py`:

- `get_run_dir(run_id)` — `.spawnd/runs/<run_id>/`
- `get_db_path(run_id)` — SQLite file
- `get_worktrees_dir(run_id)` — worktree root
- `get_logs_dir(run_id)` — logs root
- `ensure_log_file(run_id, agent_name)` — create/open per-agent log

### Coordination Tools

Implementations live in `spawnd/tools/worker.py` and `spawnd/tools/manager.py`. They are wrapped as in-process MCP tools via `spawnd/tools/factory.py` (`create_worker_tools`, `create_manager_tools`). Both worker and manager tools write directly to SQLite via `spawnd.storage.db`; blocking tools (`request_clarification`, `report_blocker`) poll the `responses` table.

### Programmatic Runs

```python
import asyncio
from spawnd import run, agent

result = asyncio.run(run(
    [
        agent("a", "first step", check="true"),
        agent("b", "second step", check="true", depends_on=["a"]),
    ],
    name="my-run",
    use_mock=True,  # dev-only: skip SDK calls
))
print(result.run_id, result.success, result.completed)
```

After the call, `spawnd status <result.run_id>` / `spawnd logs <result.run_id>` / `spawnd merge <result.run_id>` all work as if the run had been started from the CLI.
