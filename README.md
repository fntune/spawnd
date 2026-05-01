# spawnd.dev

> Multi-agent orchestration for Claude Code. Parallel worktrees. Resumable runs.

```bash
spawnd run -f plan.yaml
spawnd dashboard <run-id>
spawnd merge <run-id>
```

Write a YAML plan. Spawnd resolves dependencies, spawns agents in isolated git worktrees, tracks everything in SQLite, and merges branches when done. Crash mid-run? `spawnd resume`.

---

## Why

Chaining Claude sessions by hand doesn't scale. Copy-pasting outputs between windows, manually rebasing branches, losing all context when a session dies — this is friction that shouldn't exist.

Spawnd treats multi-agent work as a **data structure**: a plan spec with named agents, dependency edges, and completion conditions. Every run is tracked, every branch is isolated, every failure is recoverable.

- **Declarative plans.** YAML specs with `depends_on` edges. Spawnd resolves topological order and runs independent agents in parallel.
- **Worktree isolation.** Each agent gets its own git worktree and branch. No file conflicts between parallel agents. Merge when done.
- **Resume-first.** Every run is a SQLite record. `spawnd resume <run-id>` re-enters from the last known state — agents that completed stay completed.
- **Failure modes.** Per-agent: `continue`, `stop`, or `retry` (with error context injected back into the retry prompt). Circuit breaker trips on threshold.
- **Roles.** Seven built-in role templates (architect, implementer, tester, reviewer, debugger, refactorer, documenter) with specialized prompts and default completion checks.

---

## Installation

```bash
pip install -e ".[sdk]"   # with Claude Agent SDK
pip install -e .           # without (use --mock for dry runs)
```

---

## Quick start

Write a plan:

```yaml
# plan.yaml
name: auth-feature

agents:
  - name: design
    use_role: architect
    prompt: "Design the JWT auth middleware. Output a spec with interface contracts."

  - name: implement
    use_role: implementer
    prompt: "Implement the JWT auth middleware from design's spec."
    depends_on: [design]
    check: "cargo build"

  - name: test
    use_role: tester
    prompt: "Write unit and integration tests for the auth middleware."
    depends_on: [implement]
    check: "cargo test auth"

  - name: review
    use_role: reviewer
    prompt: "Review the implementation and tests."
    depends_on: [implement, test]
    model: opus
```

Run it:

```bash
spawnd run -f plan.yaml        # launch all agents
spawnd dashboard <run-id>      # live status
spawnd logs <run-id> -a test   # stream agent logs
spawnd merge <run-id>           # merge completed branches
```

Or skip the file for quick tasks:

```bash
# single agent
spawnd run -p "audit: Find all SQL injection risks in the codebase"

# sequential pipeline
spawnd run -p "find: List all deprecated API usages" \
          -p "fix: Apply fixes from find's output" \
          --sequential
```

---

## Python API

The same scheduler is available as a library — no YAML required. Runs started from Python are indistinguishable from CLI runs: same `.spawnd/runs/<run-id>/` directory, same SQLite state, inspectable via `spawnd status`, `spawnd logs`, `spawnd merge`, `spawnd resume`.

```python
import asyncio
from spawnd import run, pipeline, handoff, agent

# DAG with dependencies
asyncio.run(run([
    agent("design",    "Spec out JWT auth middleware",   use_role="architect"),
    agent("implement", "Build it",                       use_role="implementer", depends_on=["design"]),
    agent("test",      "Write tests for it",             use_role="tester",      depends_on=["implement"], check="cargo test auth"),
], name="auth-feature"))

# Sequential sugar — pipeline auto-chains depends_on by list order
asyncio.run(pipeline([
    agent("generate", "Write a fibonacci function"),
    agent("review",   "Review the function above", use_role="reviewer"),
]))

# Two-step handoff
asyncio.run(handoff(
    agent("impl",  "Build the cache layer"),
    agent("audit", "Audit the implementation", use_role="reviewer"),
))
```

All scheduler features (retries, circuit breaker, manager spawn, blocking worker↔manager coordination, resume) work identically from both the CLI and the Python API.

---

## Plan spec

```yaml
name: plan-name

defaults:
  model: sonnet          # claude-sonnet-4-6 by default
  on_failure: continue   # continue | stop | retry

agents:
  - name: agent-name
    prompt: "Task description"
    use_role: implementer       # optional built-in role
    depends_on: [other-agent]   # wait for these to complete
    check: "pytest tests/"      # shell command; must exit 0
    on_failure: retry           # override per-agent
    model: opus                 # override per-agent
```

Agents with no `depends_on` run immediately in parallel. Agents with `depends_on` wait until all listed agents complete.

---

## Built-in roles

| Role | System prompt focus | Default check |
|------|---------------------|---------------|
| `architect` | Design, specs, interfaces | — (uses Opus) |
| `implementer` | Implement from spec, commit often | — |
| `tester` | Coverage, happy paths + edge cases | `pytest` |
| `reviewer` | Correctness, security, clarity | — |
| `debugger` | Reproduce, root cause, minimal repro | — |
| `refactorer` | Code quality, no behavior changes | lint + type check |
| `documenter` | Accurate, maintainable docs | — |

---

## Commands

| Command | Description |
|---------|-------------|
| `spawnd run -f plan.yaml` | Execute a plan spec |
| `spawnd run -p "name: task"` | Inline single agent |
| `spawnd run ... --sequential` | Force sequential execution |
| `spawnd run ... --mock` | Dry run without API calls |
| `spawnd resume <run-id>` | Resume from last known state |
| `spawnd status [run-id]` | Run status (latest if no ID) |
| `spawnd dashboard <run-id>` | Live status view |
| `spawnd logs <run-id> -a <agent>` | Stream agent logs |
| `spawnd logs <run-id> --all` | All agent logs |
| `spawnd merge <run-id>` | Merge completed branches |
| `spawnd merge <run-id> --dry-run` | Preview merge |
| `spawnd cancel <run-id>` | Cancel running agents |
| `spawnd clean [run-id]` | Remove artifacts |
| `spawnd db <run-id> [query]` | Query run state in SQLite |
| `spawnd roles [name]` | List / inspect roles |

---

## How it works

```
you write a plan spec
        │
        ▼
spawnd resolves dependency graph (topological sort)
        │
        ├─── independent agents → launch in parallel, each in its own worktree
        │
        └─── dependent agents → wait for dependencies, then launch
                │
                ▼
        each agent runs with:
          - its own git worktree (branch: agent-{name})
          - worker tool set: mark_complete, request_clarification,
                             report_progress, report_blocker
                │
                ▼
        completion: check command passes → branch ready
        failure: on_failure policy applies (continue/stop/retry)
                │
                ▼
spawnd merge: consolidate branches → resolve conflicts → done
```

Manager agents (type: manager) run with a direct API loop — full context control, can spawn subagents and read worker events before each turn. Worker agents run via the SDK Agent class for autonomous task execution.

---

## Architecture

```
spawnd/
├── cli.py          10 Click commands, entry point
├── models/         AgentSpec, PlanSpec, Defaults (Pydantic)
├── core/
│   └── deps.py     Dependency graph, topological sort, cycle detection
├── runtime/
│   ├── scheduler.py  Parallel execution, circuit breaker, stuck detection
│   └── executor.py   Agent execution (SDK + MCP tools)
├── gitops/
│   ├── git.py        Worktree creation, branch management
│   └── merge.py      Branch consolidation, conflict handling
├── io/
│   └── logs.py       Log file management
├── storage/
│   └── db.py         SQLite state (WAL mode, concurrent-safe)
├── roles.py          7 built-in role templates
└── tools.py          Worker + manager coordination tools
```

---

## Status

Beta. All 10 CLI commands implemented, 7 roles built in, SQLite persistence with WAL mode, worktree isolation, circuit breaker. Test plans with `--mock` to validate specs without API calls.
