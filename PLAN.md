# spawnd.dev - Revised DX Model & Agent Strategy

## Design Principles

1. **CLI-first**: Rich CLI is the primary interface, designed for humans AND agents
2. **Plan specs as input**: Agents write YAML specs, CLI executes them
3. **Single slash command**: `/spawnd` passes through to CLI (thin wrapper)
4. **Dual execution model**: Managers run via manual loop (full context control), workers via SDK Agent class
5. **Worktree isolation**: Every agent gets a worktree
6. **Tool-based completion**: Agents signal completion via `mark_complete()` tool, not text markers
7. **Blocking coordination**: Workers can block on manager responses for clarifications
8. **Run-scoped state**: Every run gets a unique `run_id` with resumable state and namespaced artifacts

## Terminology

| Term | Meaning |
|------|---------|
| **type** | `worker` or `manager` - determines if agent can spawn subagents |
| **use_role** | Optional specialized role template (architect, implementer, etc.) |
| **worker** | Executes a single task, cannot spawn subagents |
| **manager** | Can spawn subagents via `/spawnd run`, receives worker events |
| **role** | Predefined behavior template with system prompt, allowed tools, model |
| **template** | Problem-type pattern (feature, bugfix) that expands to full plan spec |

**Example combinations:**
- `type: worker, use_role: implementer` - Worker using implementer template
- `type: manager, use_role: architect` - Manager using architect template (can spawn workers)
- `type: worker` (no use_role) - Basic worker with custom prompt

## Architecture

### Dual Execution Model

**Managers** run via manual loop for full context control:
- Direct API calls, construct prompts dynamically
- Read worker events before each turn
- Can inject guidance, adjust strategy mid-flight

**Workers** run via SDK Agent class with standard toolset:
- Autonomous execution
- Standard toolset for reporting (mark_complete, request_clarification, etc.)
- Tools write to event bus / state files

```
┌─────────────────────────────────────────────────────────┐
│  Manager/Orchestrator (Manual Loop)                     │
│  ─────────────────────────────────────────────────────  │
│  • Direct API calls, full context control               │
│  • Reads worker events before each turn                 │
│  • Constructs prompt with: task + events + state        │
│  • Can inject guidance, adjust strategy mid-flight      │
│  • Toolset: spawn_worker, respond_to_clarification, etc.│
└─────────────────────────────────────────────────────────┘
        │ spawns                    ▲ events
        ▼                           │
┌─────────────────────────────────────────────────────────┐
│  Workers (SDK Agent + Standard Toolset)                 │
│  ─────────────────────────────────────────────────────  │
│  • Autonomous execution via Agent class                 │
│  • Standard toolset for reporting                       │
│  • Tools write to event bus / state files               │
│  • Toolset: mark_complete, request_clarification, etc.  │
└─────────────────────────────────────────────────────────┘
```

### System Overview

```
┌──────────────────────────────────────────────────────┐
│           Any Agent (Claude Code, custom, etc.)       │
│                                                       │
│   1. Analyzes task                                    │
│   2. Writes plan spec: .spawnd/plans/feature.yaml     │
│   3. Invokes: /spawnd run feature.yaml                │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  /spawnd <args>  │  ← Single slash command
              │  (thin wrapper) │     passes through to CLI
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────────────────────────┐
              │           spawnd CLI                  │
              │         (primary DX)                 │
              │                                      │
              │  spawnd run plan.yaml                 │
              │  spawnd status                        │
              │  spawnd logs auth -f                  │
              │  spawnd cancel auth                   │
              │  spawnd merge                         │
              │  spawnd dashboard                     │
              └────────────────┬────────────────────┘
                               │
         ┌─────────────────────┴─────────────────────┐
         │                                           │
         ▼                                           ▼
┌─────────────────────┐                 ┌─────────────────────┐
│  Manual Loop        │                 │  Claude Agent SDK   │
│  (for managers)     │                 │  (for workers)      │
└─────────┬───────────┘                 └─────────┬───────────┘
          │                                       │
          ▼                                       ▼
   ┌───────────┐                    ┌─────────────────────────┐
   │  manager  │                    │     Worker Agents       │
   │  worktree │──── events ────────┤  auth │ cache │ logging │
   │           │◄─── responses ─────│       │       │         │
   └───────────┘                    └─────────────────────────┘
          │                                       │
          └───────────────────────────────────────┘
                       │
                        .spawnd/runs/{run_id}/spawnd.db
```

## Agent Toolsets

### Worker Toolset (Standard)

Every worker agent is deployed with this standard toolset for coordination:

| Tool | Behavior | Blocking |
|------|----------|----------|
| `mark_complete(summary)` | Runs check command at completion gate → passes: done, fails: returns error | No |
| `request_clarification(question, escalate_to?)` | Emits event, waits for response. `escalate_to`: "parent" \| "human" \| "auto" (default) | **Yes** |
| `report_progress(status, milestone?)` | Emits progress event; optional `milestone` marks named checkpoint | No |
| `report_blocker(question)` | Emits blocker event, waits for guidance | **Yes** |

**Note:** Coordination tools are always allowed for all agents, regardless of any role `allowed_tools` restrictions.

**Note:** Checks only run when `mark_complete()` is called; successful completion cascades to dependent agents and manager orchestration.

**Milestones:** Use `report_progress(status, milestone="core_impl")` to mark named checkpoints.
Milestones are recorded as events and help managers track progress across workers.

**Blocking flow for `request_clarification`:**

```
Worker                              Manager (manual loop)
───────                             ────────────────────
request_clarification("JWT?")  →    [event: clarification(id=abc123, agent=auth)]
     │                                      │
     ▼                                      ▼
 [BLOCKED]                         Sees event in next turn via
 status='blocked'                  get_pending_clarifications()
 polling responses table           decides response:
     │                                      │
     │                              ┌───────┴───────┐
     │                              ▼               ▼
     │                  respond_to_clarification   (if escalate_to="human",
     │                   ("abc123", "Use JWT")      forward to human dashboard)
     │                              │
     │                              ▼
     │                    [INSERT INTO responses]
     ◄──────────────────────────────┘
     │
  [UNBLOCKED]
  status='running'
  receives "Use JWT" as tool result
  continues execution
```

**Implementation detail**: The `request_clarification` tool:
1. Inserts event into `events` table
2. Updates agent status to `blocked`
3. Polls `responses` table for manager's response (indexed query)
4. When response found, marks it consumed and returns to agent

```python
def request_clarification(question: str, timeout: int = 300) -> str:
    """Blocking call - waits for manager response."""
    # 1. Emit event
    clarification_id = uuid4().hex
    run_id = current_run_id()
    db.execute(
        "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'clarification', ?)",
        (clarification_id, run_id, current_agent_name(), json.dumps({
            "question": question,
            "root_agent": root_agent_name(),
            "parent_agent": parent_agent_name(),
            "tree_path": tree_path(),
        }))
    )
    db.execute(
        "UPDATE agents SET status = 'blocked' WHERE run_id = ? AND name = ?",
        (run_id, current_agent_name())
    )
    db.commit()

    # 2. Poll for response
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.execute("""
            SELECT id, response FROM responses
            WHERE run_id = ? AND clarification_id = ? AND consumed = 0 LIMIT 1
        """, (run_id, clarification_id)).fetchone()

        if row:
            db.execute(
                "UPDATE responses SET consumed = 1 WHERE run_id = ? AND id = ?",
                (run_id, row[0])
            )
            db.execute(
                "UPDATE agents SET status = 'running' WHERE run_id = ? AND name = ?",
                (run_id, current_agent_name())
            )
            db.commit()
            return row[1]

        time.sleep(2)  # Poll every 2s

    # Timeout: update status and emit error event
    db.execute(
        "UPDATE agents SET status = 'timeout' WHERE run_id = ? AND name = ?",
        (run_id, current_agent_name())
    )
    db.execute(
        "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'error', ?)",
        (uuid4().hex, run_id, current_agent_name(), json.dumps({"error": "Clarification timeout", "question": question}))
    )
    db.commit()
    raise TimeoutError("No response from manager")

def respond_to_clarification(clarification_id: str, response: str) -> None:
    """Manager responds to a worker's clarification request, unblocking them."""
    run_id = current_run_id()
    db.execute(
        "INSERT INTO responses (run_id, clarification_id, response) VALUES (?, ?, ?)",
        (run_id, clarification_id, response)
    )
    db.commit()


def get_pending_clarifications() -> list[dict]:
    """Get all clarifications and blockers awaiting manager response."""
    run_id = current_run_id()
    rows = db.execute("""
        SELECT e.id, e.agent, e.event_type, json_extract(e.data, '$.question') as question
        FROM events e
        WHERE e.run_id = ? AND e.event_type IN ('clarification', 'blocker')
        AND NOT EXISTS (
            SELECT 1 FROM responses r WHERE r.clarification_id = e.id
        )
    """, (run_id,)).fetchall()
    return [{"id": r[0], "agent": r[1], "type": r[2], "question": r[3]} for r in rows]
```

### Manager Toolset

Managers use these tools to coordinate workers:

| Tool | Behavior |
|------|----------|
| `spawn_worker(name, prompt, check?)` | Creates SDK agent in worktree with standard toolset |
| `respond_to_clarification(clarification_id, response)` | Writes response, unblocks waiting worker |
| `cancel_worker(name)` | Terminates worker agent |
| `get_worker_status(name?)` | Returns current state of worker(s) |
| `get_pending_clarifications()` | Returns list of {id, agent, question} awaiting response |
| `mark_plan_complete(summary)` | Signals orchestration done, runs manager check (if defined) |

**Note:** Clarification events include a unique `clarification_id`. Managers see this ID in the event
and use it to respond precisely. This avoids ambiguity if an agent has multiple pending requests.

**Manager loop pseudocode:**

```python
while not done:
    # 1. Gather events from all workers
    events = read_worker_events()

    # 2. Construct prompt with task + events + current state
    prompt = build_prompt(task, events, worker_states)

    # 3. Call API
    response = call_claude_api(prompt, manager_toolset)

    # 4. Execute tool calls (spawn, respond, cancel, etc.)
    for tool_call in response.tool_calls:
        execute_tool(tool_call)

    # 5. Check for completion
    if response.calls_mark_plan_complete:
        done = True
```

## Plan Spec Format

Full schema with all options including orchestration:

```yaml
# .spawnd/plans/feature-auth.yaml
name: feature-auth                      # Required: plan identifier
description: "Implement user auth"      # Optional: human description

# Run metadata (auto-generated if omitted)
run:
  id: "2024-03-19T12-30-45Z"            # Optional: set to resume a prior run
  resume: false                         # true = resume this run_id

# Plan-level defaults (can be overridden per-agent)
defaults:
  max_iterations: 30
  check: "pytest"                       # Completion gate: must pass for mark_complete() to succeed
  on_failure: continue                  # continue | stop | retry
  retry_count: 3                        # Max retries before marking failed
  model: sonnet                         # sonnet | opus | haiku
  max_cost_usd: 5.0                     # Per-agent cost limit
  # Note: stuck detection configured in events.stuck section

# Plan-level cost budget
cost_budget:
  total_usd: 25.0                       # Total budget for entire plan
  on_exceed: pause                      # pause | cancel | warn

# Shared context files (all agents can read these)
shared_context:
  - "CLAUDE.md"
  - "docs/architecture.md"

# Orchestration settings
orchestration:
  # Manager receives events inline in prompt
  event_injection: true

  # Circuit breaker
  circuit_breaker:
    threshold: 3                        # Stop if >N agents fail
    action: cancel_all                  # cancel_all | pause | notify_only

  # Dependency context settings (for depends_on branch merges)
  dependency_context:
    mode: full                          # full | diff_only | paths
    # For diff_only: only merge files changed by the dependency
    # For paths: only merge files matching the patterns below
    include_paths:                      # Only used when mode=paths
      - "src/**/*.py"
      - "tests/**/*.py"
    exclude_paths:                      # Excluded from merge (all modes)
      - "**/__pycache__/**"
      - "*.pyc"

  # Merge settings
  merge:
    target_branch: null                 # null = auto-detect default branch (main/master)
    strategy: bottom_up                 # bottom_up | root_only
    on_conflict: spawn_resolver         # spawn_resolver | fail | manual
    resolver_timeout: 120               # Seconds before resolver times out
    resolver_max_cost: 2.0              # USD limit for resolver agent
    fallback: manual                    # What to do if resolver fails: manual | fail
    auto_cleanup: true                  # Delete worktrees after merge

# Agent definitions
agents:
  - name: auth                          # Required: agent identifier
    type: worker                        # worker | manager (can spawn subagents)
    use_role: implementer               # Optional: use built-in role template
    prompt: |                           # Required: task description
      Implement JWT authentication with refresh tokens.
      - Create auth service in src/services/auth.py
      - Add login/logout endpoints
      - Implement token refresh logic

      When complete, call mark_complete() tool.
      If blocked, call request_clarification() or report_blocker().

    # Optional overrides (inherit from defaults if not specified)
    max_iterations: 50
    check: "pytest tests/auth/"
    on_failure: retry
    retry_count: 5
    model: opus                         # Override for complex task

    # Dependencies (automatic context inheritance)
    depends_on: []                      # Files from dep branches auto-injected

    # Environment variables
    env:
      DEBUG: "true"

    # Events this agent should emit
    milestones:
      - name: core_impl
        description: "Core implementation complete"
      - name: tests_written
        description: "Tests written and passing"

  - name: cache
    type: worker
    use_role: implementer
    prompt: |
      Implement Redis session cache.
      - Create cache service in src/services/cache.py
      - Add TTL support
      - Implement cache invalidation
    check: "pytest tests/cache/"

  - name: integration
    type: worker
    use_role: integrator
    prompt: |
      Integrate auth with cache, add middleware.
      - Wire auth to use cache for sessions
      - Add auth middleware to API routes
    depends_on: [auth, cache]           # Auto-inherits files from auth + cache branches
    check: "pytest"

# Plan completion options
on_complete: merge                      # merge | none | notify
```

### Run Identity & Resumability

- `run.id` namespaces all state: `.spawnd/runs/{run_id}/` (db, worktrees, logs, telemetry, plan snapshot).
- Managers can spawn new runs with fresh `run_id` values; context inheritance remains via branches and plan snapshots.
- Branches are namespaced as `spawnd/{run_id}/{agent}` to avoid collisions across runs.
- Resume loads `.spawnd/runs/{run_id}/plan.yaml`, reuses worktrees/branches, and skips already-completed agents.
- CLI: `spawnd run plan.yaml --run-id <id>` or `spawnd run plan.yaml --resume --run-id <id>` (alias: `spawnd resume <id>`).

### Manager Agent Plan Spec

When an agent has `type: manager`, it can spawn subagents:

```yaml
name: feature-complex
description: "Complex feature requiring decomposition"

agents:
  - name: architect
    type: manager                       # Can spawn subagents
    use_role: architect                 # Uses architect role template
    prompt: |
      You are a software architect.

      Task: Implement user authentication system

      Process:
      1. Explore the codebase
      2. Break down into subtasks
      3. Write a plan spec: .spawnd/plans/architect-tasks.yaml (include run.id if resuming)
      4. Spawn workers: /spawnd run architect-tasks.yaml --run-id <id>
      5. Monitor via events (injected into your context)
      6. Respond to stuck workers with guidance
      7. Review completed work
      8. Merge: /spawnd merge
      9. Call mark_plan_complete() when done

    # Manager settings
    manager:
      max_subagents: 5                  # Limit how many workers
      event_poll_interval: 10           # Seconds between event checks
      guidance_enabled: true            # Can respond to stuck workers

    check: "pytest && ruff check"

orchestration:
  event_injection: true                 # Critical for manager
```

### Nested Hierarchy Naming

When managers spawn workers, names are hierarchical:

```yaml
# Root plan spawns 'architect' manager
# architect manager spawns: auth, cache, integration
# Resulting agent names:
#   - architect
#   - architect.auth
#   - architect.cache
#   - architect.integration
#
# If auth is also a manager and spawns: tokens, validation
#   - architect.auth.tokens
#   - architect.auth.validation
```

**Note:** Agent names stay hierarchical; run identity is separate and namespaces paths/branches.

### Event Configuration

```yaml
# Configure which events agents emit
events:
  # Standard events
  standard:
    - started        # Emitted by orchestrator on agent spawn
    - progress       # Emitted via report_progress() tool
    - clarification  # Emitted via request_clarification() tool
    - blocker        # Emitted via report_blocker() tool
    - done           # Emitted via mark_complete() on success
    - error          # Emitted by orchestrator on failure/timeout

  # Custom milestones (emitted via report_progress with milestone param)
  milestones:
    - name: exploration_complete
      description: "Agent has finished exploring the codebase"
    - name: implementation_done
      description: "Core implementation is complete"

  # Stuck detection (automatic, no text markers needed)
  stuck:
    idle_iterations: 10                 # Auto-detect after N iterations without progress
    # Progress is tracked via report_progress() calls and iteration updates
```

**Note:** All events are tool-based. No text markers are used for detection.

## Specialized Roles & Templates

Roles define behavior templates (system prompts, tool restrictions, models).
Agents specify `use_role: <role_name>` to inherit these settings.
Coordination tools are always allowed and do not need to be listed in `allowed_tools`.

**Note:** A role's `can_spawn: true` means the role is designed for managers.
The agent must still have `type: manager` to actually spawn subagents.

### Built-in Role Library

Predefined roles with tailored capabilities:

```yaml
# Built-in roles (spawnd/roles/*.yaml)
roles:
  architect:
    description: "Decomposes problems, designs solutions"
    system_prompt: |
      You are a software architect. Your job is to:
      1. Analyze requirements
      2. Design solutions
      3. Break down into implementable tasks
      4. Create plan specs for workers
    allowed_tools: [Read, Glob, Grep, Write]  # No Bash - planning only
    can_spawn: true                            # Can create subagents
    model: opus                                # Complex reasoning needed

  implementer:
    description: "Writes production code"
    system_prompt: |
      You are a code implementer. Your job is to:
      1. Read existing code to understand patterns
      2. Implement the specified feature/fix
      3. Follow project conventions
      4. Ensure check command passes
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    can_spawn: false
    model: sonnet

  tester:
    description: "Writes and runs tests"
    system_prompt: |
      You are a test engineer. Your job is to:
      1. Understand what needs testing
      2. Write comprehensive tests
      3. Run tests and ensure they pass
      4. Report coverage
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    can_spawn: false
    check: "pytest --cov"

  reviewer:
    description: "Reviews code for quality"
    system_prompt: |
      You are a code reviewer. Your job is to:
      1. Review changes for correctness
      2. Check for edge cases
      3. Verify tests are adequate
      4. Suggest improvements
    allowed_tools: [Read, Glob, Grep]  # Read-only
    can_spawn: false
    output: "review_report.md"        # Creates review artifact

  debugger:
    description: "Diagnoses and fixes issues"
    system_prompt: |
      You are a debugging expert. Your job is to:
      1. Reproduce the issue
      2. Identify root cause
      3. Propose fix
      4. Verify fix works
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    can_spawn: false

  refactorer:
    description: "Improves code structure"
    system_prompt: |
      You are a refactoring specialist. Your job is to:
      1. Understand current code structure
      2. Identify improvement opportunities
      3. Refactor while preserving behavior
      4. Ensure tests still pass
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    can_spawn: false
    check: "pytest"  # Must not break existing tests

  integrator:
    description: "Combines work from multiple sources"
    system_prompt: |
      You are an integration specialist. Your job is to:
      1. Review work from multiple agents
      2. Resolve conflicts
      3. Ensure components work together
      4. Run integration tests
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    can_spawn: false
    check: "pytest tests/integration/"
```

### Custom Role Definition

Users can define custom roles:

```yaml
# In plan spec
custom_roles:
  security_auditor:
    description: "Reviews code for security issues"
    system_prompt: |
      You are a security expert. Review for:
      - Injection vulnerabilities
      - Authentication issues
      - Data exposure
    allowed_tools: [Read, Glob, Grep]
    output: "security_report.md"

  performance_optimizer:
    description: "Identifies and fixes performance issues"
    system_prompt: |
      You are a performance engineer. Your job is to:
      1. Profile the code
      2. Identify bottlenecks
      3. Optimize critical paths
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]
    check: "python benchmark.py"
```

### Problem-Type Templates

Built-in templates expand to full plan specs. Templates define:
- Which roles to use
- Execution order (parallel groups, sequential phases)
- Dependencies between agents

```yaml
# templates/feature.yaml - New Feature Development
name: feature
description: "Full feature development pipeline"

# Templates define phases that expand to agents
phases:
  - name: design
    agents:
      - name: architect
        type: manager
        use_role: architect

  - name: implement
    parallel: true                    # All agents in this phase run in parallel
    depends_on: [design]
    agents:
      - name: "impl-{component}"      # Placeholder for each component
        type: worker
        use_role: implementer

  - name: test
    depends_on: [implement]
    agents:
      - name: tester
        type: worker
        use_role: tester

  - name: finalize
    depends_on: [test]
    agents:
      - name: integrator
        type: worker
        use_role: integrator
      - name: reviewer
        type: worker
        use_role: reviewer

# templates/bugfix.yaml - Bug Fix Pipeline
name: bugfix
description: "Diagnose and fix a bug"
phases:
  - name: diagnose
    agents:
      - name: debugger
        type: worker
        use_role: debugger
  - name: fix
    depends_on: [diagnose]
    agents:
      - name: fixer
        type: worker
        use_role: implementer
  - name: verify
    depends_on: [fix]
    agents:
      - name: verifier
        type: worker
        use_role: tester
```

### Using Templates

```bash
# Use built-in template
spawnd run --template feature "Implement user authentication"

# Template expands to full plan spec
# Architect analyzes task, spawns appropriate implementers
```

### Template-Based Plan Spec

```yaml
# .spawnd/plans/auth-feature.yaml
name: auth-feature
template: feature                    # Use built-in template

# Template parameters
params:
  components:
    - name: jwt_tokens
      description: "JWT token generation and validation"
    - name: password_hashing
      description: "Secure password hashing"
    - name: auth_service
      description: "Main authentication service"
      depends_on: [jwt_tokens, password_hashing]

# Template expands to:
# architect → jwt_tokens (impl) + password_hashing (impl) + auth_service (impl)
#          → tester → integrator → reviewer
```

### Dynamic Tree Generation

Architect agents can dynamically generate implementation trees:

```yaml
agents:
  - name: planner
    type: manager
    use_role: architect
    prompt: |
      Analyze this task and create an implementation tree.

      Task: {{task_description}}

      Based on your analysis:
      1. Identify components needed
      2. Choose appropriate roles for each
      3. Define dependencies
      4. Write plan spec to .spawnd/plans/generated.yaml
      5. Execute: /spawnd run generated.yaml

    # Architect has freedom to design the tree
    tree_generation:
      allowed_roles: [implementer, tester, reviewer, refactorer]
      max_depth: 3
      max_agents: 10
```

### Context Inheritance Flow

```
1. Agent 'auth' starts
   - Prompt includes: shared_context files (CLAUDE.md, docs/architecture.md)
   - Works in: .spawnd/runs/{run_id}/worktrees/auth/
   - Commits to: spawnd/{run_id}/auth branch

2. Agent 'cache' starts (parallel with auth)
   - Prompt includes: shared_context files
   - Works in: .spawnd/runs/{run_id}/worktrees/cache/

3. Agent 'integration' waits for deps to complete, then:
   - Worktree created from main
   - spawnd/{run_id}/auth branch MERGED into worktree (files on disk!)
   - spawnd/{run_id}/cache branch MERGED into worktree (files on disk!)
   - Prompt includes: shared_context files
   - Agent can import auth/cache code, run tests
   - Works in: .spawnd/runs/{run_id}/worktrees/integration/
```

**Key change:** Dependency code is merged into the worktree, not just injected as text.
This enables real imports, tests, and integration work.

## CLI Commands

### Execution

```bash
# Execute plan spec
spawnd run plan.yaml
spawnd run .spawnd/plans/feature-auth.yaml

# Explicit run id (namespaces all artifacts)
spawnd run plan.yaml --run-id 2024-03-19T12-30-45Z

# Resume existing run
spawnd run plan.yaml --resume --run-id 2024-03-19T12-30-45Z

# Inline execution with -p flag
spawnd run -p "auth: Implement JWT auth" -p "cache: Implement cache"

# Sequential pipeline (--seq)
spawnd run --seq -p "plan: Create implementation plan" \
                -p "impl: Execute the plan" \
                -p "review: Review the code"

# Pattern-based (one agent per file)
spawnd run --each "src/services/*.py" -p "Add type hints to {file}"

# Override defaults
spawnd run plan.yaml --max-iterations 50 --check "make test"
```

### Monitoring

```bash
spawnd status                 # Table if TTY, JSON if piped
spawnd status auth            # Single agent details
spawnd status --json          # Force JSON output
spawnd status --run-id <id>   # Inspect a specific run
spawnd logs auth              # View agent logs
spawnd logs auth -f           # Follow mode (tail -f)
spawnd logs auth --run-id <id>
spawnd logs --all             # Interleaved logs from all agents
spawnd dashboard              # TUI with live updates
spawnd dashboard --run-id <id>
```

### Control

```bash
spawnd cancel                 # Cancel all agents (latest run)
spawnd cancel --run-id <id>   # Cancel specific run
spawnd cancel auth cache      # Cancel specific agents
spawnd merge                  # Merge all completed branches (latest run)
spawnd merge --run-id <id>    # Merge specific run
spawnd merge auth cache       # Merge specific branches
spawnd resume <id>            # Resume run (alias for spawnd run --resume --run-id)
spawnd resume <id> --agent auth  # Resume a specific agent within a run
spawnd clean                  # Remove stale worktrees, reset DB (latest run)
spawnd clean --run-id <id>    # Clean specific run
spawnd clean --all            # Full cleanup including logs/telemetry
spawnd db                     # Open interactive SQLite shell (latest run)
spawnd db --run-id <id>       # Open DB for specific run
spawnd db "SELECT * FROM agents WHERE run_id = '<id>'"  # Run SQL query
```

### Status Output Format

```
# TTY output (human-readable table)
┌──────────────┬──────────┬──────┬────────────────┬─────────┐
│ Agent        │ Status   │ Iter │ Branch         │ Check   │
├──────────────┼──────────┼──────┼────────────────┼─────────┤
│ auth         │ ✓ done   │ 8/30 │ spawnd/{run_id}/auth     │ ✓ pass  │
│ cache        │ ● run    │ 3/30 │ spawnd/{run_id}/cache    │ …       │
│ integration  │ ○ wait   │ 0/30 │ —              │ —       │
└──────────────┴──────────┴──────┴────────────────┴─────────┘

# JSON output (machine-readable)
{
  "agents": [
    {"name": "auth", "status": "completed", "iteration": 8, ...},
    {"name": "cache", "status": "running", "iteration": 3, ...},
    {"name": "integration", "status": "pending", "iteration": 0, ...}
  ]
}
```

## Slash Command (Single)

One slash command, passes through to CLI:

```bash
/spawnd run plan.yaml         # → spawnd run plan.yaml
/spawnd status                # → spawnd status
/spawnd logs auth -f          # → spawnd logs auth -f
/spawnd cancel auth           # → spawnd cancel auth
/spawnd merge                 # → spawnd merge
/spawnd dashboard             # → spawnd dashboard
```

This enables any agent (Claude Code, custom agents) to orchestrate full pipelines:

```
Agent thinking: "This feature needs auth, cache, and integration work.
I'll write a plan spec and spawn parallel agents."

Agent action:
1. Write .spawnd/plans/feature-auth.yaml
2. Run: /spawnd run feature-auth.yaml
3. Monitor: /spawnd status
4. Consolidate: /spawnd merge
```

## Agent Execution Model

### Lifecycle

```
1. Run setup
   - Assign run_id (if not provided)
   - Snapshot plan: .spawnd/runs/{run_id}/plan.yaml
   - Initialize DB: .spawnd/runs/{run_id}/spawnd.db

2. Spawn
   - Create worktree: .spawnd/runs/{run_id}/worktrees/{name}/
   - Create branch: spawnd/{run_id}/{name}
   - Inject shared_context files
   - Inject dep branch files (if depends_on specified)
   - Insert agent record into DB (status: pending)
   - Launch SDK agent with prompt

3. Run
   - Agent iterates on task
   - Updates state each iteration (status: running)
   - Commits changes to branch

4. Check Gate (via mark_complete tool)
   - Agent calls mark_complete(summary)
   - Tool runs check command (e.g., pytest)
   - If fails: returns error, agent continues iterating
   - If passes: agent marked complete

5. Complete
   - mark_complete() succeeds
   - State updated (status: completed)
   - Dependent agents unblocked

6. Merge
   - User runs /spawnd merge
   - Changes merged to main
   - Worktree + branch cleaned up
   - State archived
```

### Completion Flow

```
┌─────────────────────────────────────────────────────────┐
│                    Agent Iteration                       │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Agent working on task │
              └──────────┬───────────┘
                         │
              ┌──────────▼────────────────┐
              │ Agent calls mark_complete?│
              └──────────┬────────────────┘
                    No   │   Yes
         ┌───────────────┴────────────────┐
         │                                │
         ▼                                ▼
┌─────────────────┐             ┌─────────────────┐
│ Max iterations? │             │ Run check cmd   │
└────────┬────────┘             └────────┬────────┘
    No   │   Yes                   Pass  │  Fail
         │    │                          │    │
         ▼    ▼                          ▼    │
      Continue  Mark                  Mark    │
      iteration timeout            completed  │
                                              │
                         ┌────────────────────┘
                         ▼
              ┌──────────────────────────────┐
              │ Return error to agent,       │
              │ agent continues iterating    │
              └──────────────────────────────┘
```

### Failure Handling

| Condition | on_failure=continue | on_failure=stop | on_failure=retry |
|-----------|---------------------|-----------------|------------------|
| Check fails | Continue iterating | Continue iterating | Continue iterating |
| Max iterations | Mark timeout, continue others | Cancel all agents | Restart agent (up to retry_count) |
| Agent error | Mark failed, continue others | Cancel all agents | Restart agent |
| Dep failed | Skip this agent | Cancel all agents | Wait/retry dep |

### Error Context for Retries

When an agent is retried, it receives:
```
Previous attempt failed with:
- Error: [last error message]
- Iteration reached: 15/30
- Check output: [last check command output]

Please continue from where you left off, addressing the error above.
```

### Database Architecture

All state is stored in SQLite per run (`.spawnd/runs/{run_id}/spawnd.db`) instead of JSON files:

| Problem | File-Based | SQLite |
|---------|-----------|--------|
| Atomicity | Partial writes corrupt JSON | ACID transactions |
| Concurrency | Race conditions | Proper locking |
| Querying | Read all files | `WHERE status = 'blocked'` |
| Blocking tools | Poll files | Poll indexed table |
| Cost tracking | Sum across files | `SUM(cost_usd)` |

**Why SQLite:**
- Zero config - single file, no server
- Built into Python stdlib
- ACID transactions
- WAL mode = concurrent reads + serialized writes
- Sufficient for <100 agents

**Concurrency Configuration:**

```python
def open_db(run_id: str) -> sqlite3.Connection:
    """Open DB with proper concurrency settings."""
    db_path = Path(f".spawnd/runs/{run_id}/spawnd.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(db_path, timeout=30.0)  # Wait up to 30s for locks
    db.row_factory = sqlite3.Row

    # Enable WAL mode for concurrent reads
    db.execute("PRAGMA journal_mode = WAL")
    # Wait up to 5s for busy locks (inside transactions)
    db.execute("PRAGMA busy_timeout = 5000")
    # Sync less often for performance (data safe due to WAL)
    db.execute("PRAGMA synchronous = NORMAL")

    return db
```

**Best practices:**
- Each agent monitor task should have its own connection
- Use `with db:` for automatic commit/rollback
- Keep transactions short to minimize lock contention
- Retry on `sqlite3.OperationalError` with "database is locked"

### Database Schema

```sql
-- .spawnd/runs/{run_id}/spawnd.db (WAL mode enabled)

CREATE TABLE plans (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,              -- plan name
    spec TEXT NOT NULL,              -- YAML content
    status TEXT DEFAULT 'running',   -- running | completed | failed | paused
    total_cost_usd REAL DEFAULT 0.0,
    max_cost_usd REAL DEFAULT 25.0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agents (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    plan_name TEXT,                  -- denormalized for convenience
    status TEXT NOT NULL,            -- pending | running | blocked | checking | paused | completed | failed | timeout | cancelled | cost_exceeded
    iteration INTEGER DEFAULT 0,
    max_iterations INTEGER DEFAULT 30,
    worktree TEXT,
    branch TEXT,
    prompt TEXT,
    check_command TEXT,
    model TEXT DEFAULT 'sonnet',
    parent TEXT,                     -- Hierarchical: manager.auth.tokens
    session_id TEXT,                 -- SDK resumption
    pid INTEGER,
    cost_usd REAL DEFAULT 0.0,
    max_cost_usd REAL DEFAULT 5.0,
    error TEXT,
    depends_on TEXT,                 -- JSON array of agent names this agent depends on
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, name),
    FOREIGN KEY (run_id) REFERENCES plans(run_id)
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    agent TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- started | progress | clarification | blocker | done | error | cascade_skip | circuit_breaker_tripped
    data TEXT,                       -- JSON
    FOREIGN KEY (run_id, agent) REFERENCES agents(run_id, name)
);

CREATE TABLE responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    clarification_id TEXT NOT NULL,
    response TEXT NOT NULL,
    consumed INTEGER DEFAULT 0,      -- 1 when worker has read it
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (clarification_id) REFERENCES events(id)
);

-- Indexes for common queries
CREATE INDEX idx_agents_run_status ON agents(run_id, status);
CREATE INDEX idx_agents_run_parent ON agents(run_id, parent);
CREATE INDEX idx_events_run_agent ON events(run_id, agent);
CREATE INDEX idx_events_run_type ON events(run_id, event_type);
CREATE INDEX idx_responses_pending ON responses(run_id, clarification_id, consumed) WHERE consumed = 0;
```

### Common Queries

```sql
-- All blocked agents in a run (waiting for clarification/response)
SELECT name, status FROM agents
WHERE run_id = ? AND status = 'blocked';

-- Total cost for a run
SELECT SUM(cost_usd) FROM agents WHERE run_id = ?;

-- Check if run exceeds budget
SELECT name FROM plans
WHERE run_id = ? AND total_cost_usd > max_cost_usd;

-- Pending clarifications/blockers (manager needs to answer)
SELECT e.id, e.agent, e.event_type, json_extract(e.data, '$.question') as question
FROM events e
WHERE e.run_id = ? AND e.event_type IN ('clarification', 'blocker')
AND NOT EXISTS (
    SELECT 1 FROM responses r WHERE r.clarification_id = e.id
);

-- Agent hierarchy
SELECT name, parent FROM agents WHERE run_id = ? AND parent IS NOT NULL;

-- Recent events for dashboard
SELECT * FROM events WHERE run_id = ? ORDER BY ts DESC LIMIT 100;
```

## Claude Agent SDK Integration

**Architecture Decision:** Based on SDK research (Jan 2026), we use:
- **Workers**: Independent `query()` calls wrapped in `asyncio.create_task()` for true parallelism
- **Managers**: `ClaudeSDKClient` for stateful multi-turn conversations with event injection
- **Coordination Tools**: In-process MCP servers via `@tool` decorator with direct DB access
- **Hierarchy**: Up to 10 levels supported (not using SDK subagents which cap at 2 levels)

**Why not SDK subagents?** The SDK's Task tool subagents:
1. Block until completion (no true parallelism)
2. Are invoked by Claude's decision, not imperatively
3. Limited to 2 nesting levels
4. Cannot spawn their own subagents

Instead, we spawn workers as separate `query()` calls, giving full control over parallelism and hierarchy.

### Coordination Tools (In-Process MCP)

Custom tools run in the same process, with direct SQLite access for coordination:

```python
import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4
from claude_agent_sdk import tool, create_sdk_mcp_server, query, ClaudeAgentOptions

# Database connection (thread-local for async safety)
_db_local = threading.local()

def get_db() -> sqlite3.Connection:
    """Get thread-local DB connection."""
    if not hasattr(_db_local, "conn"):
        run_id = os.environ["SPAWND_RUN_ID"]
        _db_local.conn = sqlite3.connect(f".spawnd/runs/{run_id}/spawnd.db")
        _db_local.conn.row_factory = sqlite3.Row
    return _db_local.conn


@tool(
    name="mark_complete",
    description="Signal task completion. Runs check command automatically.",
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary of work completed"}
        },
        "required": ["summary"]
    }
)
async def mark_complete(args: dict) -> dict:
    """Worker signals completion - runs check gate."""
    db = get_db()
    agent_name = os.environ["SPAWND_AGENT_NAME"]
    run_id = os.environ["SPAWND_RUN_ID"]

    # Get check command from DB
    row = db.execute(
        "SELECT check_command FROM agents WHERE run_id = ? AND name = ?",
        (run_id, agent_name)
    ).fetchone()
    check_cmd = row["check_command"] or "pytest"

    # Run check command
    result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        # Success - mark completed
        db.execute(
            "UPDATE agents SET status = 'completed' WHERE run_id = ? AND name = ?",
            (run_id, agent_name)
        )
        db.execute(
            "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'done', ?)",
            (uuid4().hex, run_id, agent_name, json.dumps({"summary": args["summary"]}))
        )
        db.commit()
        return {"content": [{"type": "text", "text": "Task completed successfully. Check passed."}]}
    else:
        # Failed - return error, agent continues
        return {"content": [{"type": "text", "text": f"Check failed. Fix and retry.\n\nOutput:\n{result.stdout}\n{result.stderr}"}]}


@tool(
    name="request_clarification",
    description="Ask manager for guidance. BLOCKS until response received.",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Question to ask manager"},
            "escalate_to": {
                "type": "string",
                "enum": ["parent", "human", "auto"],
                "description": "Who to escalate to (default: auto)"
            }
        },
        "required": ["question"]
    }
)
async def request_clarification(args: dict, timeout: int = 300) -> dict:
    """Blocking call - polls DB for manager response."""
    db = get_db()
    agent_name = os.environ["SPAWND_AGENT_NAME"]
    run_id = os.environ["SPAWND_RUN_ID"]
    clarification_id = uuid4().hex

    # Emit clarification event
    db.execute(
        "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'clarification', ?)",
        (clarification_id, run_id, agent_name, json.dumps({
            "question": args["question"],
            "escalate_to": args.get("escalate_to", "auto"),
            "parent_agent": os.environ.get("SPAWND_PARENT_AGENT"),
            "tree_path": os.environ.get("SPAWND_TREE_PATH"),
        }))
    )
    db.execute(
        "UPDATE agents SET status = 'blocked' WHERE run_id = ? AND name = ?",
        (run_id, agent_name)
    )
    db.commit()

    # Poll for response
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = db.execute("""
            SELECT id, response FROM responses
            WHERE run_id = ? AND clarification_id = ? AND consumed = 0 LIMIT 1
        """, (run_id, clarification_id)).fetchone()

        if row:
            db.execute("UPDATE responses SET consumed = 1 WHERE id = ?", (row["id"],))
            db.execute(
                "UPDATE agents SET status = 'running' WHERE run_id = ? AND name = ?",
                (run_id, agent_name)
            )
            db.commit()
            return {"content": [{"type": "text", "text": f"Manager response: {row['response']}"}]}

        await asyncio.sleep(2)

    # Timeout
    db.execute(
        "UPDATE agents SET status = 'timeout' WHERE run_id = ? AND name = ?",
        (run_id, agent_name)
    )
    db.commit()
    return {"content": [{"type": "text", "text": "ERROR: Clarification timeout. No response from manager."}]}


@tool(
    name="report_progress",
    description="Report progress update. Use milestone param for named checkpoints.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Current status"},
            "milestone": {"type": "string", "description": "Optional milestone name (e.g., 'core_impl')"}
        },
        "required": ["status"]
    }
)
async def report_progress(args: dict) -> dict:
    """Non-blocking progress update."""
    db = get_db()
    agent_name = os.environ["SPAWND_AGENT_NAME"]
    run_id = os.environ["SPAWND_RUN_ID"]

    db.execute(
        "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'progress', ?)",
        (uuid4().hex, run_id, agent_name, json.dumps(args))
    )
    db.commit()
    return {"content": [{"type": "text", "text": "Progress recorded."}]}


@tool(
    name="report_blocker",
    description="Report blocking issue. BLOCKS until manager responds with guidance.",
    input_schema={
        "type": "object",
        "properties": {
            "issue": {"type": "string", "description": "Description of the blocking issue"}
        },
        "required": ["issue"]
    }
)
async def report_blocker(args: dict, timeout: int = 300) -> dict:
    """Blocking call for blockers - similar to clarification."""
    # Reuse clarification logic with different event type
    args["question"] = args.pop("issue")
    args["escalate_to"] = "parent"
    # ... same polling logic as request_clarification
    return await request_clarification(args, timeout)


# Create MCP server with coordination tools
def create_coordination_server():
    """Create in-process MCP server with coordination tools."""
    return create_sdk_mcp_server(
        name="spawnd-coordination",
        version="1.0.0",
        tools=[mark_complete, request_clarification, report_progress, report_blocker]
    )
```

### Worker Options Builder

```python
from dataclasses import dataclass


@dataclass
class AgentConfig:
    """Agent configuration from plan or DB."""
    name: str
    prompt: str
    check_command: str = "pytest"
    depends_on: list[str] = None
    model: str = "sonnet"
    max_iterations: int = 30
    max_cost_usd: float = 5.0
    worktree_path: Path = None
    shared_context: str = ""
    env: dict = None
    parent: str = None  # For hierarchy tracking

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentConfig":
        """Create AgentConfig from database row."""
        return cls(
            name=row["name"],
            prompt=row["prompt"],
            check_command=row["check_command"] or "pytest",
            depends_on=json.loads(row["depends_on"] or "[]"),
            model=row["model"] or "sonnet",
            max_iterations=row["max_iterations"] or 30,
            max_cost_usd=row["max_cost_usd"] or 5.0,
            worktree_path=Path(row["worktree"]) if row["worktree"] else None,
            parent=row["parent"],
        )

    def tree_path(self) -> str:
        """Get full hierarchy path (e.g., 'manager.auth.tokens')."""
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name


def build_worker_options(config: AgentConfig, run_id: str) -> ClaudeAgentOptions:
    """Build SDK options for a worker agent."""
    return ClaudeAgentOptions(
        cwd=str(config.worktree_path),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        mcp_servers={"coordination": create_coordination_server()},
        permission_mode="bypassPermissions",
        model=config.model,
        max_turns=config.max_iterations,
        env={
            "SPAWND_RUN_ID": run_id,
            "SPAWND_AGENT_NAME": config.name,
            "SPAWND_PARENT_AGENT": config.parent or "",
            "SPAWND_TREE_PATH": config.tree_path(),
            **(config.env or {}),
        },
        system_prompt=f"""You are an autonomous coding agent.

Task: {config.prompt}

Check command: {config.check_command}

Coordination tools available:
- mark_complete(summary): Call when task is done. Runs check command automatically.
- request_clarification(question, escalate_to?): Ask for guidance (BLOCKS until response).
- report_progress(status, milestone?): Report progress updates.
- report_blocker(issue): Report blocking issues (BLOCKS until response).

{config.shared_context}
""",
    )


def parse_plan_spec(yaml_content: str) -> PlanSpec:
    """Parse YAML content into PlanSpec."""
    data = yaml.safe_load(yaml_content)
    # Validate hierarchy depth
    validate_hierarchy_depth(data.get("agents", []))
    return PlanSpec(**data)


def validate_hierarchy_depth(agents: list, current_depth: int = 1, max_depth: int = 10) -> None:
    """Validate agent hierarchy doesn't exceed max depth."""
    if current_depth > max_depth:
        raise ValueError(f"Agent hierarchy exceeds maximum depth of {max_depth} levels")
    # Hierarchy depth is tracked via parent field in DB, validated at spawn time
```

### Worker Spawning (via query())

Workers are spawned as independent `query()` calls wrapped in asyncio tasks for true parallelism:

```python
from claude_agent_sdk import query, ClaudeAgentOptions
from typing import AsyncIterator
import logging

logger = logging.getLogger("spawnd")


async def run_worker(
    config: AgentConfig,
    run_id: str,
    db: sqlite3.Connection,
) -> dict:
    """Run a worker agent to completion. Returns result dict."""

    options = build_worker_options(config, run_id)

    # Track cost and iterations
    total_cost = 0.0
    iteration = 0

    try:
        async for message in query(prompt=config.prompt, options=options):
            iteration += 1

            # Update iteration count in DB
            db.execute(
                "UPDATE agents SET iteration = ? WHERE run_id = ? AND name = ?",
                (iteration, run_id, config.name)
            )
            db.commit()

            # Log output
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        log_to_file(run_id, config.name, block.text)

            # Track cost from result message
            if hasattr(message, "total_cost_usd"):
                total_cost = message.total_cost_usd
                db.execute(
                    "UPDATE agents SET cost_usd = ? WHERE run_id = ? AND name = ?",
                    (total_cost, run_id, config.name)
                )

            # Check for errors
            if hasattr(message, "is_error") and message.is_error:
                db.execute(
                    "UPDATE agents SET status = 'failed', error = ? WHERE run_id = ? AND name = ?",
                    (str(message.result), run_id, config.name)
                )
                db.execute(
                    "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'error', ?)",
                    (uuid4().hex, run_id, config.name, json.dumps({"error": str(message.result)}))
                )
                db.commit()
                return {"success": False, "error": str(message.result), "cost": total_cost}

        # Check final status (coordination tools update status directly)
        final_status = db.execute(
            "SELECT status FROM agents WHERE run_id = ? AND name = ?",
            (run_id, config.name)
        ).fetchone()["status"]

        return {
            "success": final_status == "completed",
            "status": final_status,
            "cost": total_cost,
            "iterations": iteration,
        }

    except Exception as e:
        logger.error(f"Worker {config.name} failed: {e}")
        db.execute(
            "UPDATE agents SET status = 'failed', error = ? WHERE run_id = ? AND name = ?",
            (str(e), run_id, config.name)
        )
        db.commit()
        return {"success": False, "error": str(e), "cost": total_cost}


async def spawn_worker(
    config: AgentConfig,
    run_id: str,
    db: sqlite3.Connection,
) -> asyncio.Task:
    """Spawn worker as background asyncio task. Returns task handle."""

    # Update DB status to running
    db.execute(
        "UPDATE agents SET status = 'running' WHERE run_id = ? AND name = ?",
        (run_id, config.name)
    )
    db.execute(
        "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'started', ?)",
        (uuid4().hex, run_id, config.name, json.dumps({"prompt": config.prompt[:200]}))
    )
    db.commit()

    # Create and return asyncio task
    return asyncio.create_task(
        run_worker(config, run_id, db),
        name=f"worker-{config.name}"
    )
```

### Manager Loop (via ClaudeSDKClient)

Managers use stateful `ClaudeSDKClient` for multi-turn conversations with event injection:

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions


def build_manager_options(config: AgentConfig, run_id: str) -> ClaudeAgentOptions:
    """Build SDK options for a manager agent."""
    return ClaudeAgentOptions(
        cwd=str(config.worktree_path),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        mcp_servers={"coordination": create_coordination_server()},
        permission_mode="bypassPermissions",
        model=config.model or "opus",  # Managers typically need stronger reasoning
        env={
            "SPAWND_RUN_ID": run_id,
            "SPAWND_AGENT_NAME": config.name,
            "SPAWND_TREE_PATH": config.tree_path(),
            **(config.env or {}),
        },
        system_prompt=f"""You are a manager agent coordinating worker agents.

Task: {config.prompt}

You can spawn workers, monitor their progress, and respond to their questions.

Manager tools:
- spawn_worker(name, prompt, check?): Create a new worker agent
- respond_to_clarification(clarification_id, response): Answer a worker's question
- cancel_worker(name): Stop a worker agent
- get_worker_status(name?): Get status of worker(s)
- get_pending_clarifications(): Get questions awaiting your response
- mark_plan_complete(summary): Signal orchestration is done

{config.shared_context}
""",
    )


async def run_manager(
    config: AgentConfig,
    run_id: str,
    db: sqlite3.Connection,
) -> dict:
    """Run manager agent with event injection loop."""

    options = build_manager_options(config, run_id)

    async with ClaudeSDKClient(options=options) as client:
        # Initial prompt
        await client.query(config.prompt)

        while True:
            # Process responses
            async for message in client.receive_response():
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text"):
                            log_to_file(run_id, config.name, block.text)

                # Check if manager called mark_plan_complete
                if hasattr(message, "is_error"):
                    break

            # Check if manager is done
            status = db.execute(
                "SELECT status FROM agents WHERE run_id = ? AND name = ?",
                (run_id, config.name)
            ).fetchone()["status"]

            if status == "completed":
                return {"success": True}

            # Inject events into next turn
            events = get_recent_events(db, run_id)
            clarifications = get_pending_clarifications(db, run_id)

            if not events and not clarifications:
                await asyncio.sleep(5)  # Poll interval
                continue

            # Build event summary for manager
            event_summary = format_events_for_manager(events, clarifications)
            await client.query(f"Events update:\n{event_summary}\n\nRespond to any pending clarifications or adjust strategy as needed.")

    return {"success": False, "error": "Manager loop exited unexpectedly"}


def get_recent_events(db: sqlite3.Connection, run_id: str, since_seconds: int = 30) -> list[dict]:
    """Get events from the last N seconds."""
    rows = db.execute("""
        SELECT agent, event_type, data, ts FROM events
        WHERE run_id = ? AND ts > datetime('now', ?)
        ORDER BY ts DESC LIMIT 50
    """, (run_id, f"-{since_seconds} seconds")).fetchall()
    return [dict(r) for r in rows]


def get_pending_clarifications(db: sqlite3.Connection, run_id: str) -> list[dict]:
    """Get clarifications awaiting manager response."""
    rows = db.execute("""
        SELECT e.id, e.agent, json_extract(e.data, '$.question') as question
        FROM events e
        WHERE e.run_id = ? AND e.event_type IN ('clarification', 'blocker')
        AND NOT EXISTS (SELECT 1 FROM responses r WHERE r.clarification_id = e.id)
    """, (run_id,)).fetchall()
    return [dict(r) for r in rows]


def format_events_for_manager(events: list[dict], clarifications: list[dict]) -> str:
    """Format events for injection into manager prompt."""
    lines = []
    if clarifications:
        lines.append("## Pending Clarifications (respond with respond_to_clarification tool)")
        for c in clarifications:
            lines.append(f"- [{c['id'][:8]}] {c['agent']}: {c['question']}")
    if events:
        lines.append("\n## Recent Events")
        for e in events:
            lines.append(f"- [{e['ts']}] {e['agent']}: {e['event_type']}")
    return "\n".join(lines) or "No new events."
```

### How Coordination Works

With in-process MCP tools, coordination is handled **inside the tool functions** (see Coordination Tools section above):

1. **Tool execution**: SDK automatically executes tools when Claude calls them
2. **mark_complete**: Tool runs check command, updates DB status, returns result to Claude
3. **request_clarification**: Tool emits event, polls DB, blocks until response, returns to Claude
4. **report_progress**: Tool writes event to DB, returns immediately

**No manual tool result handling required** - the SDK manages the tool call → result → continue loop.

```
┌─────────────────────────────────────────────────────────┐
│                    Worker (query())                      │
│                                                          │
│  1. Claude works on task                                │
│  2. Claude calls mark_complete(summary)                 │
│  3. SDK executes @tool mark_complete                    │
│     → Tool runs check command                           │
│     → Tool updates DB (completed/running)               │
│     → Tool returns result string                        │
│  4. SDK sends result back to Claude                     │
│  5. If check passed → query() ends                      │
│     If check failed → Claude continues iterating        │
└─────────────────────────────────────────────────────────┘
```

### Agent Context Helpers

```python
def get_root_agent(db: sqlite3.Connection, run_id: str, agent_name: str) -> str:
    """Get the root manager agent for an agent hierarchy."""
    current = agent_name
    while True:
        row = db.execute(
            "SELECT parent FROM agents WHERE run_id = ? AND name = ?",
            (run_id, current)
        ).fetchone()
        if not row or not row["parent"]:
            return current
        current = row["parent"]


def get_parent_agent(db: sqlite3.Connection, run_id: str, agent_name: str) -> str | None:
    """Get the immediate parent agent, or None if root."""
    row = db.execute(
        "SELECT parent FROM agents WHERE run_id = ? AND name = ?",
        (run_id, agent_name)
    ).fetchone()
    return row["parent"] if row else None


def get_tree_path(db: sqlite3.Connection, run_id: str, agent_name: str) -> str:
    """Get the full hierarchy path (e.g., 'manager.auth.tokens')."""
    path = [agent_name]
    current = agent_name
    while True:
        parent = get_parent_agent(db, run_id, current)
        if not parent:
            break
        path.insert(0, parent)
        current = parent
    return ".".join(path)


def log_to_file(run_id: str, agent_name: str, text: str) -> None:
    """Append agent output to log file."""
    log_path = Path(f".spawnd/runs/{run_id}/logs/{agent_name}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {text}\n")
```

### Agent Lifecycle Management

```python
class AgentManager:
    """Manages multiple concurrent agents."""

    def __init__(self):
        self.clients: dict[str, ClaudeSDKClient] = {}

    async def spawn(self, config: AgentConfig) -> None:
        """Spawn agent in background."""
        client = ClaudeSDKClient(options=build_options(config))
        self.clients[config.name] = client

        # Start in background task
        asyncio.create_task(self._run_agent(config.name, client))

    async def cancel(self, name: str) -> None:
        """Gracefully stop an agent."""
        if name in self.clients:
            await self.clients[name].interrupt()
            await self.clients[name].disconnect()
            del self.clients[name]

    async def resume(self, name: str, session_id: str) -> None:
        """Resume a crashed/stopped agent."""
        state = load_state(name)
        options = build_options_from_state(state)
        options.resume = session_id  # Resume from session

        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.clients[name] = client
```

### Completion Detection

Completion is detected via the `mark_complete` tool call, not text parsing:

```python
# Completion is handled in the tool use handler (see Agent Monitoring above)
# When agent calls mark_complete(summary):
#   1. Check command runs
#   2. If passes: agent marked complete, tool returns success
#   3. If fails: tool returns error with check output, agent continues

# The ResultMessage indicates agent termination (may or may not be completion)
async for message in client.receive_response():
    if isinstance(message, ResultMessage):
        result = {
            "success": not message.is_error,
            "output": message.result,
            "session_id": message.session_id,
            "cost_usd": message.total_cost_usd,
            "duration_ms": message.duration_ms,
        }

        if message.is_error:
            # Agent errored out
            state.status = "failed"
        elif state.status != "completed":
            # Agent stopped without completing (max turns, etc.)
            state.status = "timeout"
```

### Session Persistence

For crash recovery and resumption:

```python
# Save session_id to DB
db.execute(
    "UPDATE agents SET session_id = ? WHERE run_id = ? AND name = ?",
    (client.session_id, run_id, agent_name)
)
db.commit()

# Later: resume from session
row = db.execute(
    "SELECT session_id, worktree FROM agents WHERE run_id = ? AND name = ?",
    (run_id, agent_name)
).fetchone()

options = ClaudeAgentOptions(
    resume=row["session_id"],  # Resume from checkpoint
    cwd=row["worktree"],
)
client = ClaudeSDKClient(options=options)
await client.connect("Continue the task")
```

### Worktree Management

```python
def create_worktree(run_id: str, agent_name: str, repo_path: Path = None) -> Path:
    """Create a git worktree for an agent.

    Args:
        run_id: The run identifier
        agent_name: Name of the agent
        repo_path: Path to git repo (defaults to cwd if None)

    Returns:
        Path to the created worktree
    """
    repo = repo_path or Path.cwd()
    worktree_path = repo / "worktrees" / run_id / agent_name
    branch_name = f"spawnd/{run_id}/{agent_name}"

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=repo,
        check=True,
    )

    return worktree_path
```

### Dependency Branch Merging

When an agent has `depends_on`, dependency branches are **merged into the worktree** at spawn time.
This ensures dependent agents can actually import and test code from their dependencies.

```python
def setup_worktree_with_deps(
    run_id: str,
    agent_name: str,
    depends_on: list[str],
    worktree_path: Path,
    context_config: DependencyContextConfig,
) -> None:
    """Merge dependency branches into agent's worktree."""

    for dep_name in depends_on:
        dep_branch = f"spawnd/{run_id}/{dep_name}"

        if context_config.mode == "full":
            # Full merge - all files from dep branch
            merge_branch(worktree_path, dep_branch, dep_name)

        elif context_config.mode == "diff_only":
            # Only cherry-pick files that were changed by the dep agent
            base_branch = get_default_branch()
            changed_files = get_changed_files(dep_branch, base_branch)
            cherry_pick_files(worktree_path, dep_branch, changed_files)

        elif context_config.mode == "paths":
            # Only merge files matching include_paths
            merge_branch_filtered(
                worktree_path, dep_branch, dep_name,
                include=context_config.include_paths,
                exclude=context_config.exclude_paths,
            )


def merge_branch(worktree_path: Path, branch: str, dep_name: str) -> None:
    """Full merge of a dependency branch."""
    result = subprocess.run(
        ["git", "merge", branch, "--no-edit", "-m", f"Merge {dep_name} dependency"],
        cwd=worktree_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise DependencyMergeError(
            f"Conflict merging {dep_name}. Output: {result.stderr.decode()}"
        )


def get_changed_files(branch: str, base: str) -> list[str]:
    """Get files changed between base and branch."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{branch}"],
        capture_output=True, text=True
    )
    return result.stdout.strip().split("\n")


def get_default_branch() -> str:
    """Detect the default branch (main or master)."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        # refs/remotes/origin/main -> main
        return result.stdout.strip().split("/")[-1]
    # Fallback: check if main or master exists
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True
        )
        if result.returncode == 0:
            return branch
    return "main"  # Default assumption
```

**Note:** `depends_on` now means:
1. **Ordering**: Wait for dep agents to complete before spawning
2. **Code inheritance**: Merge dep branches into worktree (files exist on disk)
3. **Prompt context**: Only `shared_context` files are injected into prompt

**Context modes:**
- `full`: Merge entire dep branch (default, simplest)
- `diff_only`: Only files changed by dep agent (smaller, avoids unrelated changes)
- `paths`: Only files matching include_paths patterns (most selective)

This allows dependent agents to:
- Import modules created by dependencies
- Run tests that use dependency code
- Build on actual code, not just text descriptions

## Scheduler Architecture

### Overview

The scheduler is **CLI-driven** with **in-process async execution**:
- `spawnd run` starts the scheduler
- Workers run as independent `query()` calls in asyncio tasks
- Managers run via `ClaudeSDKClient` with event injection
- Scheduler polls DB for completion, handles cost limits

```
┌─────────────────────────────────────────────────────────┐
│                    spawnd run plan.yaml                   │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │      Scheduler       │
              │   (async event loop) │
              └──────────┬───────────┘
                         │
         ┌───────────────┼───────────────┐
         │               │               │
         ▼               ▼               ▼
   ┌───────────┐   ┌───────────┐   ┌───────────┐
   │  Worker 1 │   │  Worker 2 │   │  Manager  │
   │  (task)   │   │  (task)   │   │  (task)   │
   │           │   │           │   │           │
   │  query()  │   │  query()  │   │  SDK      │
   │           │   │           │   │  Client   │
   └───────────┘   └───────────┘   └───────────┘
        ▲               ▲               │
        │               │               ▼
        └───────────────┴───── Events (SQLite) ─────
```

### Scheduler Implementation

```python
class Scheduler:
    """Orchestrates agent execution using SQLite for state."""

    def __init__(self, db: sqlite3.Connection, plan: PlanSpec):
        self.db = db
        self.plan = plan
        # Auto-generate run_id if not provided
        self.run_id = plan.run.id if plan.run and plan.run.id else f"{plan.name}-{uuid4().hex[:8]}"
        self.tasks: dict[str, asyncio.Task] = {}  # Active asyncio tasks

        # Initialize plan in DB
        db.execute(
            "INSERT INTO plans (run_id, name, spec, max_cost_usd) VALUES (?, ?, ?, ?)",
            (self.run_id, plan.name, yaml.dump(plan.dict()), plan.cost_budget.total_usd if plan.cost_budget else 25.0)
        )
        db.commit()

    async def run(self) -> SchedulerResult:
        """Execute plan until all agents complete."""

        while not self._all_done():
            # Find agents ready to start (deps completed, status=pending)
            ready = self.db.execute("""
                SELECT a.* FROM agents a
                WHERE a.run_id = ? AND a.status = 'pending'
                AND NOT EXISTS (
                    SELECT 1 FROM agents dep
                    WHERE dep.run_id = a.run_id
                    AND dep.name IN (SELECT value FROM json_each(a.depends_on))
                    AND dep.status NOT IN ('completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded')
                )
            """, (self.run_id,)).fetchall()

            # Check for failed dependencies - cascade failure
            for row in ready:
                deps = json.loads(row["depends_on"] or "[]")
                if deps:
                    failed_deps = self.db.execute("""
                        SELECT name FROM agents
                        WHERE run_id = ? AND name IN ({}) AND status IN ('failed', 'timeout', 'cancelled', 'cost_exceeded')
                    """.format(",".join("?" * len(deps))), (self.run_id, *deps)).fetchall()
                    if failed_deps:
                        self.db.execute(
                            "UPDATE agents SET status = 'failed', error = ? WHERE run_id = ? AND name = ?",
                            (f"Dependency failed: {[d['name'] for d in failed_deps]}", self.run_id, row["name"])
                        )
                        self.db.execute(
                            "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'cascade_skip', ?)",
                            (uuid4().hex, self.run_id, row["name"], json.dumps({"failed_deps": [d["name"] for d in failed_deps]}))
                        )
                        self.db.commit()
                        continue

            # Spawn ready agents as asyncio tasks
            for row in ready:
                if row["name"] not in self.tasks:
                    task = await self._spawn_agent(row)
                    self.tasks[row["name"]] = task

            # Clean up completed tasks
            for name, task in list(self.tasks.items()):
                if task.done():
                    try:
                        result = task.result()
                        logger.info(f"Agent {name} finished: {result}")
                    except Exception as e:
                        logger.error(f"Agent {name} raised exception: {e}")
                    del self.tasks[name]

            # Check cost budget
            total_cost = self.db.execute(
                "SELECT SUM(cost_usd) FROM agents WHERE run_id = ?",
                (self.run_id,)
            ).fetchone()[0] or 0

            if self.plan.cost_budget and total_cost > self.plan.cost_budget.total_usd:
                await self._handle_cost_exceeded()

            await asyncio.sleep(1)  # Poll every second

        return self._build_result()

    async def _spawn_agent(self, agent_row: sqlite3.Row) -> asyncio.Task:
        """Create worktree, merge deps, and spawn agent as asyncio task."""
        name = agent_row["name"]
        agent_type = agent_row.get("type", "worker")
        agent_config = AgentConfig.from_row(agent_row)

        # 1. Create git worktree
        worktree_path = create_worktree(self.run_id, name)
        agent_config.worktree_path = worktree_path

        # 2. Merge dependency branches into worktree (if any)
        if agent_config.depends_on:
            setup_worktree_with_deps(
                self.run_id, name, agent_config.depends_on, worktree_path,
                self.plan.orchestration.dependency_context if self.plan.orchestration else None
            )

        # 3. Update DB with worktree info
        self.db.execute("""
            UPDATE agents SET worktree = ?, branch = ?
            WHERE run_id = ? AND name = ?
        """, (str(worktree_path), f"spawnd/{self.run_id}/{name}", self.run_id, name))
        self.db.commit()

        # 4. Spawn as asyncio task based on type
        if agent_type == "manager":
            return asyncio.create_task(
                run_manager(agent_config, self.run_id, self.db),
                name=f"manager-{name}"
            )
        else:
            return await spawn_worker(agent_config, self.run_id, self.db)

    def _all_done(self) -> bool:
        """Check if all agents are in terminal state."""
        pending = self.db.execute("""
            SELECT COUNT(*) FROM agents
            WHERE run_id = ? AND status NOT IN ('completed', 'failed', 'timeout', 'cost_exceeded', 'cancelled', 'paused')
        """, (self.run_id,)).fetchone()[0]
        return pending == 0

    async def _handle_cost_exceeded(self) -> None:
        """Handle cost budget exceeded - pause all agents and plan."""
        # 1. Update plan status to paused
        self.db.execute(
            "UPDATE plans SET status = 'paused' WHERE run_id = ?",
            (self.run_id,)
        )

        # 2. Pause all running agents (let them finish current iteration)
        running_agents = self.db.execute("""
            SELECT name FROM agents
            WHERE run_id = ? AND status = 'running'
        """, (self.run_id,)).fetchall()

        for agent in running_agents:
            name = agent["name"]
            # Signal agent to pause after current iteration
            if name in self.clients:
                await self.clients[name].interrupt()
            self.db.execute(
                "UPDATE agents SET status = 'paused' WHERE run_id = ? AND name = ?",
                (self.run_id, name)
            )

        # 3. Emit cost_exceeded event
        self.db.execute(
            "INSERT INTO events (id, run_id, agent, event_type, data) VALUES (?, ?, ?, 'error', ?)",
            (str(uuid4()), self.run_id, "_system", json.dumps({
                "error": "cost_exceeded",
                "total_cost": self.db.execute(
                    "SELECT SUM(cost_usd) FROM agents WHERE run_id = ?", (self.run_id,)
                ).fetchone()[0],
                "budget": self.plan.cost_budget.total_usd,
            }))
        )
        self.db.commit()

        logging.warning(f"Run {self.run_id} paused: cost budget exceeded. Use 'spawnd resume {self.run_id} --add-budget <amount>' to continue.")
```

**Resume after cost pause:**

```bash
# Resume with additional budget
spawnd resume <run_id> --add-budget 10.0

# Or just resume (continues with current budget, will pause again if exceeded)
spawnd resume <run_id>
```

```python
async def resume_paused_run(run_id: str, add_budget: float = 0.0) -> None:
    """Resume a paused run, optionally adding budget."""
    db = open_db(run_id)

    # 1. Add budget if specified
    if add_budget > 0:
        db.execute(
            "UPDATE plans SET max_cost_usd = max_cost_usd + ? WHERE run_id = ?",
            (add_budget, run_id)
        )

    # 2. Update plan status
    db.execute("UPDATE plans SET status = 'running' WHERE run_id = ?", (run_id,))

    # 3. Resume paused agents
    paused_agents = db.execute("""
        SELECT * FROM agents WHERE run_id = ? AND status = 'paused'
    """, (run_id,)).fetchall()

    for agent_row in paused_agents:
        db.execute(
            "UPDATE agents SET status = 'running' WHERE run_id = ? AND name = ?",
            (run_id, agent_row["name"])
        )

    db.commit()

    # 4. Restart scheduler
    scheduler = Scheduler(db, parse_plan_spec(db.execute(
        "SELECT spec FROM plans WHERE run_id = ?", (run_id,)
    ).fetchone()[0]))
    await scheduler.run()
```

### Crash Recovery

The scheduler is in-process - if `spawnd run` crashes, agents die. Recovery uses **resume-from-DB**:

**State preserved on crash:**
- All agent states in SQLite (status, iteration, session_id, cost)
- Worktrees persist on disk
- Branches persist in git
- Plan snapshot in `.spawnd/runs/{run_id}/plan.yaml`

**State lost on crash:**
- In-flight work since last git commit
- SDK session state (may be recoverable via session_id)

**Resume flow:**

```bash
# Resume a crashed run
spawnd resume <run_id>
# Equivalent to: spawnd run --resume --run-id <run_id>
```

```python
async def resume_run(run_id: str) -> None:
    """Resume a run from its last known state."""
    db = open_db(run_id)

    # Load plan from snapshot
    plan_path = Path(f".spawnd/runs/{run_id}/plan.yaml")
    plan = parse_plan_spec(plan_path.read_text())

    # Find agents that need restart
    incomplete = db.execute("""
        SELECT * FROM agents
        WHERE run_id = ? AND status IN ('running', 'blocked', 'pending')
    """, (run_id,)).fetchall()

    for agent_row in incomplete:
        name = agent_row["name"]
        session_id = agent_row["session_id"]

        if session_id:
            # Try to resume SDK session
            try:
                client = ClaudeSDKClient(options=ClaudeAgentOptions(
                    resume=session_id,
                    cwd=agent_row["worktree"],
                ))
                await client.connect("Continue from where you left off")
            except SessionExpiredError:
                # Session lost, restart agent from scratch
                await restart_agent(db, run_id, agent_row)
        else:
            # No session, restart from scratch
            await restart_agent(db, run_id, agent_row)


async def restart_agent(db: sqlite3.Connection, run_id: str, agent_row: sqlite3.Row) -> None:
    """Restart an agent from its last committed state."""
    name = agent_row["name"]
    worktree = Path(agent_row["worktree"])

    # Reset iteration count (or continue from where it was)
    db.execute("""
        UPDATE agents SET status = 'running', error = NULL
        WHERE run_id = ? AND name = ?
    """, (run_id, name))

    # Worktree already has committed work - agent continues from there
    prompt = f"""Continue the task. Your previous progress is in the worktree.

Previous iteration: {agent_row['iteration']}
Previous error (if any): {agent_row['error']}

Review your previous commits and continue from where you left off.
"""

    client = ClaudeSDKClient(options=build_options(agent_row, worktree))
    await client.connect(prompt)
    asyncio.create_task(monitor_agent(db, client, run_id, name))
```

**Best practices for robustness:**
1. Agents should commit frequently (at milestones)
2. Use `report_progress()` to update DB state
3. Check commands validate actual completion

### Dependency Resolution

```python
class DependencyGraph:
    """Manages agent dependencies."""

    def __init__(self, agents: list[AgentConfig]):
        self.agents = {a.name: a for a in agents}
        self.deps = {a.name: set(a.depends_on) for a in agents}

    def get_ready_agents(self, completed: set[str]) -> list[AgentConfig]:
        """Get agents whose dependencies are satisfied."""
        ready = []
        for name, deps in self.deps.items():
            if deps.issubset(completed):
                ready.append(self.agents[name])
        return ready

    def topological_order(self) -> list[str]:
        """Get merge order (deps before dependents)."""
        # Kahn's algorithm
        in_degree = {n: len(d) for n, d in self.deps.items()}
        queue = [n for n, d in in_degree.items() if d == 0]
        order = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for name, deps in self.deps.items():
                if node in deps:
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        queue.append(name)

        return order
```

## Merge Strategy

### Merge Order

Branches merged in **dependency order** (topological sort):
- Deps merged before dependents
- Ensures consistent state

```
Example: auth, cache → integration

1. Merge spawnd/{run_id}/auth → main
2. Merge spawnd/{run_id}/cache → main
3. Merge spawnd/{run_id}/integration → main (has both deps' changes)
```

### Conflict Resolution

When merge conflicts occur, **spawn resolver agent**:

```python
async def merge_with_conflict_resolution(run_id: str, branches: list[str]) -> MergeResult:
    """Merge branches, spawning resolver on conflicts."""

    order = topological_order(branches)

    for branch in order:
        result = git_merge(branch)

        if result.has_conflicts:
            # Extract agent name from branch (spawnd/{run_id}/{agent_name})
            agent_name = branch.split("/")[-1]
            resolver_name = f"resolver-{agent_name}"

            # Spawn conflict resolver agent
            resolver = await spawn_conflict_resolver(
                run_id=run_id,
                agent_name=resolver_name,
                branch=branch,
                conflicts=result.conflict_files,
            )

            # Wait for resolution
            await resolver.wait()

            # Verify resolution
            if not git_status_clean():
                raise MergeError(f"Resolver failed for {branch}")

        # Cleanup
        delete_worktree(branch)
        delete_branch(branch)

    return MergeResult(merged=order)


async def spawn_conflict_resolver(
    run_id: str,
    agent_name: str,
    branch: str,
    conflicts: list[str],
) -> AgentRunner:
    """Spawn agent to resolve merge conflicts."""

    prompt = f"""Resolve merge conflicts in the following files:
{conflicts}

The branch being merged is: {branch}
The agent whose work is being merged: {agent_name}

1. Review each conflicted file
2. Choose the correct resolution (keep ours, theirs, or combine)
3. Stage resolved files with git add
4. Do NOT commit - just resolve and stage
5. Verify no conflict markers remain: git diff --check

Call mark_complete() when all conflicts are resolved."""

    # Use agent name (not branch) to avoid slashes in resolver name
    config = AgentConfig(
        name=f"resolver-{agent_name}",
        prompt=prompt,
        check="git diff --check && git diff --name-only --diff-filter=U | wc -l | grep -q '^0$'",
    )

    return await spawn_agent(run_id, config)
```

### Merge Flow

```
┌─────────────────────────────────────────────────────────┐
│                    spawnd merge                           │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Get topological order │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ For each branch:     │
              │   git merge branch   │
              └──────────┬───────────┘
                         │
           ┌─────────────┴─────────────┐
           │                           │
      No conflicts              Has conflicts
           │                           │
           ▼                           ▼
    ┌─────────────┐         ┌─────────────────────┐
    │  Continue   │         │ Spawn resolver agent │
    └─────────────┘         └──────────┬──────────┘
                                       │
                                       ▼
                            ┌──────────────────────┐
                            │ Wait for resolution  │
                            └──────────┬───────────┘
                                       │
                                       ▼
                            ┌──────────────────────┐
                            │ Verify & continue    │
                            └──────────────────────┘
                                       │
                                       ▼
              ┌──────────────────────┐
              │ Cleanup:             │
              │ - Delete worktree    │
              │ - Delete branch      │
              │ - Archive state      │
              └──────────────────────┘
```

## Dashboard Design

### Layout

**Split view** with agent table (top) and log viewer (bottom):

```
┌─────────────────────────────────────────────────────────────────┐
│  SPAWND DASHBOARD                                    feature-auth │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Agent         Status      Iter    Branch          Check  Cost  │
│  ──────────────────────────────────────────────────────────────  │
│  ▶ auth        ● running   12/30   spawnd/{run_id}/auth      …     $0.15  │
│    cache       ✓ done       8/30   spawnd/{run_id}/cache     ✓     $0.12  │
│    integration ○ pending    0/30   —               —     —      │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  LOGS: auth                                           [Follow ✓] │
│  ──────────────────────────────────────────────────────────────  │
│  [12:05:23] Reading src/services/auth.py...                      │
│  [12:05:24] Analyzing existing authentication patterns...        │
│  [12:05:26] Creating JWT token generation function...            │
│  [12:05:28] Writing tests for token validation...                │
│  [12:05:30] Running pytest tests/auth/...                        │
│  > 3 passed, 1 failed                                            │
│  [12:05:32] Fixing test_token_expiry...                          │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  [c]ancel  [m]erge  [l]ogs  [f]ilter  [q]uit       ↑↓ select    │
└─────────────────────────────────────────────────────────────────┘
```

### Keyboard Controls

| Key | Action |
|-----|--------|
| `↑`/`↓` | Select agent in table |
| `Enter` | View selected agent logs |
| `c` | Cancel selected agent |
| `m` | Merge completed agents |
| `l` | Toggle log panel |
| `f` | Filter agents (running/done/failed) |
| `r` | Refresh |
| `q` | Quit dashboard |

### Implementation (Textual TUI)

```python
import sqlite3
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log, Footer
from textual.containers import Vertical

class SpawndDashboard(App):
    """TUI dashboard for spawnd monitoring using SQLite."""

    BINDINGS = [
        ("c", "cancel", "Cancel"),
        ("m", "merge", "Merge"),
        ("l", "toggle_logs", "Logs"),
        ("f", "filter", "Filter"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, run_id: str, db_path: str | None = None):
        super().__init__()
        self.run_id = run_id
        if db_path is None:
            db_path = f".spawnd/runs/{run_id}/spawnd.db"
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row

    def compose(self) -> ComposeResult:
        yield Vertical(
            DataTable(id="agents"),
            Log(id="logs"),
        )
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#agents", DataTable)
        table.add_columns("Agent", "Status", "Iter", "Branch", "Check", "Cost")
        self.set_interval(1.0, self.refresh_agents)

    async def refresh_agents(self) -> None:
        """Query DB and update table."""
        table = self.query_one("#agents", DataTable)
        table.clear()

        rows = self.db.execute("""
            SELECT name, status, iteration, max_iterations, branch, cost_usd
            FROM agents
            WHERE run_id = ?
            ORDER BY created_at
        """, (self.run_id,)).fetchall()

        for row in rows:
            table.add_row(
                row["name"],
                format_status(row["status"]),
                f"{row['iteration']}/{row['max_iterations']}",
                row["branch"] or "—",
                "✓" if row["status"] == "completed" else "…",
                f"${row['cost_usd']:.2f}" if row["cost_usd"] else "—",
            )

    async def action_cancel(self) -> None:
        """Cancel selected agent."""
        table = self.query_one("#agents", DataTable)
        if table.cursor_row is not None:
            agent_name = table.get_cell_at((table.cursor_row, 0))
            self.db.execute(
                "UPDATE agents SET status = 'cancelled' WHERE run_id = ? AND name = ?",
                (self.run_id, agent_name)
            )
            self.db.commit()
            # Also kill the process
            await cancel_agent_process(agent_name)

    async def action_merge(self) -> None:
        """Merge all completed agents."""
        completed = self.db.execute(
            "SELECT name FROM agents WHERE run_id = ? AND status = 'completed'",
            (self.run_id,)
        ).fetchall()
        if completed:
            await merge_agents(self.run_id, [r["name"] for r in completed])
```

## Agent Prompt Engineering

### Hierarchical Agent Model

Agents are **orchestrators** - they can spawn subagents recursively:

```
User Request
    │
    ▼
┌────────────────────────────────────────────────┐
│              Manager Agent                      │
│                                                 │
│  1. Explore codebase                           │
│  2. Refine plan                                │
│  3. Break down into tasks                      │
│  4. Formulate dependency order                 │
│  5. Spawn subagents: /spawnd run plan.yaml      │
│  6. Monitor: /spawnd status                     │
│  7. Collate results                            │
│  8. Review work                                │
│  9. Merge: /spawnd merge                        │
│                                                 │
└─────────────────┬──────────────────────────────┘
                  │
    ┌─────────────┼─────────────┐
    │             │             │
    ▼             ▼             ▼
┌─────────┐  ┌─────────┐  ┌─────────┐
│ Worker  │  │ Worker  │  │ Sub-Mgr │ ← Can spawn its own workers
│ Agent   │  │ Agent   │  │ Agent   │
└─────────┘  └─────────┘  └────┬────┘
                               │
                    ┌──────────┼──────────┐
                    │          │          │
                    ▼          ▼          ▼
               ┌─────────┐ ┌─────────┐ ┌─────────┐
               │ Worker  │ │ Worker  │ │ Worker  │
               └─────────┘ └─────────┘ └─────────┘
```

**Nesting**: Unlimited - any agent can spawn subagents recursively.

### Manager Prompt Template (Built-in Default)

```markdown
# Manager Agent

You are an autonomous software engineering manager agent.

## Your Task
{{task_description}}

## Process

### Phase 1: Understand
1. Explore the codebase to understand existing patterns
2. Read relevant documentation (CLAUDE.md, README, etc.)
3. Identify files and modules related to the task

### Phase 2: Plan
1. Break down the task into discrete subtasks
2. Identify dependencies between subtasks
3. Determine which subtasks can run in parallel
4. Write a plan spec file

### Phase 3: Execute
1. Create plan spec: `.spawnd/plans/{{plan_name}}.yaml`
2. Spawn subagents: `/spawnd run .spawnd/plans/{{plan_name}}.yaml`
3. Monitor progress: `/spawnd status`

### Phase 4: Review
1. When all agents complete, review their work
2. Check for consistency across branches
3. Run integration tests

### Phase 5: Consolidate
1. Merge branches: `/spawnd merge`
2. Resolve any conflicts
3. Run final tests
4. Call mark_plan_complete() when done

## Checkpointing
Commit at milestones:
- After completing exploration
- After writing plan spec
- After all subagents complete
- After successful merge

## Tools Available
- `/spawnd run plan.yaml` - Execute plan with subagents
- `/spawnd status` - Check agent status
- `/spawnd merge` - Consolidate completed branches
- `/spawnd cancel` - Stop agents if needed

## Output
When task is complete, call mark_plan_complete(summary)
```

### Worker Prompt Template

```markdown
# Worker Agent

You are an autonomous coding agent focused on a specific task.

## Your Task
{{task_description}}

## Check Command
{{check_command}}

## Coordination Tools
- mark_complete(summary): Call when done. Runs check command automatically.
- request_clarification(question): Ask manager for guidance (blocks until response).
- report_progress(status): Report progress updates.
- report_blocker(issue): Report blocking issues (blocks until response).

## Process

1. Read relevant files to understand context
2. Implement the required changes
3. Write tests if applicable
4. Call mark_complete() when done (runs check automatically)

## Checkpointing
Commit at milestones:
- After understanding existing code
- After completing core implementation
- After adding tests
- After check passes

## Important
- mark_complete() runs the check command - only call when you believe task is done
- If stuck, call request_clarification() or report_blocker()
- Focus only on your assigned task
```

### Plan Spec Generation by Manager

Managers write plan specs to orchestrate workers:

```yaml
# Generated by manager agent
name: feature-user-auth
description: "Manager decomposed user auth into these subtasks"

defaults:
  max_iterations: 30
  on_failure: continue

agents:
  # Manager identified these as parallelizable
  - name: jwt-tokens
    prompt: |
      Implement JWT token generation and validation.
      - Create src/auth/tokens.py
      - Add sign_token() and verify_token() functions
      - Include refresh token logic
    check: "pytest tests/auth/test_tokens.py"

  - name: password-hashing
    prompt: |
      Implement secure password hashing.
      - Create src/auth/passwords.py
      - Use bcrypt for hashing
      - Add verify_password() function
    check: "pytest tests/auth/test_passwords.py"

  # Manager identified this depends on both above
  - name: auth-service
    prompt: |
      Integrate tokens and passwords into auth service.
      - Create src/services/auth_service.py
      - Implement login(), logout(), register()
      - Wire up JWT tokens and password hashing
    depends_on: [jwt-tokens, password-hashing]
    check: "pytest tests/services/test_auth.py"

on_complete: merge
```

### Prompt Best Practices

| Practice | Why |
|----------|-----|
| Explicit check command | Agent knows when task is complete |
| Clear deliverables | Agent knows what to produce |
| Scope boundaries | Agent stays focused |
| Dependency context | Agent understands prerequisites |
| Checkpoint guidance | Work is recoverable |

## Agent Orchestration

### Event System

Agents communicate via an event bus for real-time coordination:

```
┌─────────────────────────────────────────────────────────────┐
│                       Manager Agent                          │
│                                                              │
│  Events queried from DB before each turn                    │
│  Can respond with guidance to workers                       │
└─────────────────────────────────┬────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │       Event Bus           │
                    │   events table (SQLite)   │
                    └─────────────┬─────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
   ┌───────────┐           ┌───────────┐           ┌───────────┐
   │  Worker A │           │  Worker B │           │  Worker C │
   └───────────┘           └───────────┘           └───────────┘
```

### Event Types

```python
EVENTS = {
    "started": "Agent has begun work",
    "progress": "Agent iteration update (data.milestone for checkpoints)",
    "clarification": "Blocking question, resolved by parent agent or human",
    "blocker": "Agent is blocked, needs help (like clarification but for general issues)",
    "done": "Agent completed successfully",
    "error": "Agent encountered an error or timeout",
    "cascade_skip": "Agent skipped due to failed dependency",
    "circuit_breaker_tripped": "Circuit breaker activated, stopping agents",
}
# Note: Milestones are represented as progress events with data.milestone set.
# Example: {"event_type": "progress", "data": {"milestone": "core_impl", "status": "Impl complete"}}
```

### Event Storage

Events stored in `events` table (see Database Schema above):

```sql
-- Example event queries
-- Recent events for an agent
SELECT * FROM events WHERE agent = 'auth' ORDER BY ts DESC LIMIT 10;

-- Pending clarifications/blockers needing manager response
SELECT e.id, e.agent, e.event_type, json_extract(e.data, '$.question') as question
FROM events e
WHERE e.event_type IN ('clarification', 'blocker')
AND NOT EXISTS (
    SELECT 1 FROM responses r WHERE r.clarification_id = e.id
);

-- Event timeline
SELECT ts, agent, event_type, data FROM events ORDER BY ts;
```

```
Example event flow:
12:00:00 | auth   | started     | {"prompt": "Impl auth"}
12:01:00 | auth   | progress    | {"iteration": 1}
12:02:00 | auth   | progress    | {"milestone": "core_impl", "status": "Core implementation done"}
12:03:00 | auth   | clarification | {"question": "JWT or sessions?", "parent_agent": "architect", "tree_path": "architect.auth"}
         | (auth blocks, waiting for response)
12:03:30 | (parent agent or human inserts into responses table)
         | (auth unblocks, receives "Use JWT")
12:05:00 | auth   | done        | {"summary": "Auth complete"}
```

### Event Handling

**Manager queries events from DB** - Before each turn, manager loop queries recent events and includes them in prompt construction.

**Stuck detection** - Both explicit and automatic:
- Worker calls `report_blocker()` tool (blocks until response)
- System detects no progress for N iterations via `updated_at` column

### Manager-Worker Dynamics

```
Manager spawns workers
       │
       ▼
Workers emit events to DB (started, progress, clarification, done)
       │
       ▼
Manager queries events table before each turn
       │
       ▼
Manager can respond via respond_to_clarification() tool
       │
       ▼
Workers poll responses table, unblock when response arrives
       │
       ▼
Workers complete → Manager reviews → Merge
```

### Worker Escalation Protocol

When a worker is blocked, it uses blocking tools that handle DB operations:

```python
# Worker calls blocking tool - this is handled by the tool implementation
response = report_blocker(
    "Cannot find database configuration. "
    "Tried: config.yaml, settings.py, env vars. "
    "Need: Database connection string location"
)
# Tool inserts event, updates status to 'blocked', polls responses table
# When manager responds, tool returns the response string
# Worker continues with manager's advice in 'response'
```

The blocking is handled inside the tool implementation (see request_clarification in Agent Toolsets).

### Hierarchical Communication

**No peer-to-peer communication** - all communication flows through the manager hierarchy:

```
         Manager
        /   |   \
       /    |    \
   Worker Worker Worker
      │
      │ (no direct communication)
      │
   Worker Worker Worker
```

This keeps the architecture clean and prevents coordination complexity.

### Recursive Spawning

Agents can spawn subagents recursively (unlimited nesting).

**Flat worktrees with namespaced names:**

```
.spawnd/
└── runs/
    └── {run_id}/
        ├── spawnd.db                        # All agent state in SQLite
        ├── worktrees/
        │   ├── manager/                    # Root manager
        │   ├── manager.auth/               # Manager's worker
        │   ├── manager.cache/              # Manager's worker
        │   ├── manager.auth.tokens/        # Nested worker (auth's subagent)
        │   └── manager.auth.validation/    # Nested worker
        └── logs/
            ├── manager.log
            ├── manager.auth.log
            └── manager.auth.tokens.log
```

**Hierarchy encoded in names:**
- `manager` → root
- `manager.auth` → child of manager
- `manager.auth.tokens` → grandchild

### Nested Merge Strategy

**Bottom-up merging** - deepest workers merge first, then up to root:

```
1. manager.auth.tokens completes → stays on branch spawnd/{run_id}/manager.auth.tokens
2. manager.auth.validation completes → stays on branch
3. manager.auth (sub-manager) merges its workers:
   - Merge spawnd/{run_id}/manager.auth.tokens → spawnd/{run_id}/manager.auth
   - Merge spawnd/{run_id}/manager.auth.validation → spawnd/{run_id}/manager.auth
4. manager.cache completes
5. manager (root) merges:
   - Merge spawnd/{run_id}/manager.auth → spawnd/{run_id}/manager
   - Merge spawnd/{run_id}/manager.cache → spawnd/{run_id}/manager
6. Final: merge spawnd/{run_id}/manager → main
```

### Failure Cascades

**Manager decides on dependent failures:**

When a worker fails, its manager is notified and decides:
- Retry the worker
- Skip dependents
- Reassign work
- Abort entire subtree

```python
# Manager receives failure event
{"type": "error", "agent": "auth", "data": {"error": "Tests failed"}}

# Manager decides response
if can_retry(agent):
    emit_guidance(agent, "retry", context=extract_error_context())
elif has_workaround(agent):
    emit_guidance(agent, "try_alternative", alternative=...)
else:
    emit_event("cascade_skip", {"agents": get_dependents(agent)})
```

**Circuit breaker:**

If more than N agents fail, stop all remaining:

```yaml
# Plan spec
circuit_breaker:
  threshold: 3          # Stop if >3 agents fail
  action: cancel_all    # cancel_all | pause | notify_only
```

```python
class CircuitBreaker:
    def __init__(self, threshold: int):
        self.threshold = threshold
        self.failures = 0

    def record_failure(self) -> bool:
        self.failures += 1
        if self.failures > self.threshold:
            return True  # Trip circuit
        return False

    def on_trip(self, scheduler):
        scheduler.cancel_all_agents()
        scheduler.emit_event("circuit_breaker_tripped", {
            "failures": self.failures,
            "threshold": self.threshold,
        })
```

## Coordination Modes

### Parallel (default)
```bash
spawnd run -p "auth: Impl auth" -p "cache: Impl cache" -p "log: Impl logging"
```
- All agents spawn immediately
- Run independently in separate worktrees
- Merge when all complete

### Sequential (`--seq`)
```bash
spawnd run --seq -p "plan: Create plan" -p "impl: Execute" -p "review: Review"
```
- Each agent waits for previous to call `mark_complete()`
- Next agent starts from previous agent's branch (not main)
- Artifacts passed via committed files (PLAN.md, etc.)
- Branch chain: `main → spawnd/{run_id}/plan → spawnd/{run_id}/impl → spawnd/{run_id}/review`
- Final merge: `spawnd/{run_id}/review → main` (contains all changes)

### DAG (via plan spec)
```yaml
# .spawnd/plans/feature.yaml
name: feature-auth
agents:
  - name: auth
    prompt: "Implement JWT auth"
  - name: cache
    prompt: "Implement session cache"
  - name: integration
    prompt: "Integrate auth with cache"
    depends_on: [auth, cache]
```
```bash
spawnd run .spawnd/plans/feature.yaml
```
- Dependency resolution via topological sort
- Parallel where possible, sequential where required

### Pattern (`--each`)
```bash
spawnd run --each "src/services/*.py" -p "Add type hints to {file}"
```
- Glob expansion → N agents
- Each agent works on single file in isolated worktree
- Auto-merge when all complete


## Observability

### Log Format

Human-readable format for easy debugging:

```
# Log entry format
[TIMESTAMP] [LEVEL] [AGENT] MESSAGE

# Examples
[12:05:23] INFO  [auth] Agent spawned in .spawnd/runs/{run_id}/worktrees/auth
[12:05:24] INFO  [auth] Running prompt: Implement JWT auth...
[12:05:26] DEBUG [auth] Tool: Read src/auth/__init__.py
[12:05:28] DEBUG [auth] Tool: Write src/auth/tokens.py
[12:05:30] INFO  [auth] Running check: pytest tests/auth/
[12:05:32] WARN  [auth] Check failed: 1 test failed
[12:05:35] INFO  [auth] Iteration 5/30
[12:06:10] INFO  [auth] mark_complete() called, running check...
[12:06:11] INFO  [auth] Check passed, agent completed
```

### Log Locations

```
.spawnd/runs/{run_id}/
└── logs/
    ├── spawnd.log           # Main scheduler log
    ├── auth.log            # Per-agent logs
    ├── cache.log
    └── integration.log
```

### Log Levels

| Level | Purpose |
|-------|---------|
| DEBUG | Tool calls, detailed execution |
| INFO | Agent lifecycle, key events |
| WARN | Check failures, retries |
| ERROR | Agent failures, exceptions |

### Logging Implementation

```python
import logging
from pathlib import Path

def setup_logging(run_dir: Path) -> logging.Logger:
    """Configure logging for spawnd."""
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Main spawnd logger
    logger = logging.getLogger("spawnd")
    logger.setLevel(logging.DEBUG)

    # File handler for main log
    main_handler = logging.FileHandler(log_dir / "spawnd.log")
    main_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s [%(agent)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(main_handler)

    # Console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s [%(agent)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(console_handler)

    return logger


def get_agent_logger(name: str, run_dir: Path) -> logging.Logger:
    """Get logger for specific agent."""
    log_dir = run_dir / "logs"
    logger = logging.getLogger(f"spawnd.{name}")

    # Per-agent file handler
    handler = logging.FileHandler(log_dir / f"{name}.log")
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)

    return logger
```

### Telemetry (Prompt + Tool Trace)

Stored per run under `.spawnd/runs/{run_id}/telemetry/`:

- `prompts.jsonl` (prompt snapshots)
- `tool_calls.jsonl` (tool call inputs/outputs)
- `costs.jsonl` (per-call cost metadata)

Telemetry is configurable and should redact secrets by default.

### CLI Log Commands

```bash
# View main scheduler log
spawnd logs

# View specific agent log
spawnd logs auth

# Follow mode (tail -f)
spawnd logs auth -f

# Target a specific run
spawnd logs auth --run-id <id>

# Filter by level
spawnd logs auth --level error

# Show last N lines
spawnd logs auth -n 50

# All agents interleaved
spawnd logs --all
```

### Debugging

```bash
# Verbose mode (DEBUG level to console)
spawnd run plan.yaml --verbose

# Dry run (show what would happen without executing)
spawnd run plan.yaml --dry-run

# Show dependency graph
spawnd run plan.yaml --show-deps

# Agent 'auth' depends on nothing
# Agent 'cache' depends on nothing
# Agent 'integration' depends on: auth, cache
```

## Distribution & Installation

### Installation Methods

**1. pip (CLI tool)**
```bash
pip install spawnd.dev
spawnd --help
```

**2. Claude Code Plugin (slash commands)**
```bash
# Install plugin
claude plugins install spawnd.dev

# Or manual install
git clone https://github.com/user/spawnd.dev ~/.claude/spawnd.dev
```

### Package Structure

```
spawnd.dev/
├── pyproject.toml          # Package config
├── spawnd/                   # Python package
│   ├── __init__.py
│   ├── cli.py             # Click CLI
│   ├── ...
├── .claude-plugin/         # Claude Code plugin
│   ├── plugin.json        # Plugin manifest
│   └── commands/          # Slash commands
│       └── spawnd.md       # /spawnd command
└── README.md
```

### pyproject.toml

```toml
[project]
name = "spawnd.dev"
version = "0.1.0"
description = "Multi-agent orchestration for Claude Code"
requires-python = ">=3.10"
dependencies = [
    "click>=8.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "textual>=0.40",           # Dashboard TUI
    "claude-agent-sdk>=0.1.0", # Claude SDK
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "pytest-timeout>=2.0",
]

[project.scripts]
spawnd = "spawnd.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### Plugin Manifest

```json
// .claude-plugin/plugin.json
{
  "name": "spawnd.dev",
  "version": "0.1.0",
  "description": "Multi-agent orchestration",
  "commands": ["commands/spawnd.md"]
}
```

### Slash Command Definition

```markdown
<!-- commands/spawnd.md -->
---
name: spawnd
description: Multi-agent orchestration
arguments:
  - name: args
    description: CLI arguments
    required: false
---

# /spawnd

Pass-through to spawnd CLI.

## Usage
```
/spawnd run plan.yaml
/spawnd status
/spawnd merge
```

## Implementation

```bash
#!/bin/bash
spawnd "$@"
```
```

### User Configuration

```yaml
# ~/.claude/spawnd/config.yaml
defaults:
  max_iterations: 30
  max_agents: 5
  model: sonnet

logging:
  level: info
  dir: .spawnd/runs/{run_id}/logs

telemetry:
  enabled: true
  dir: .spawnd/runs/{run_id}/telemetry
  redact: true

git:
  worktree_dir: .spawnd/runs/{run_id}/worktrees
  branch_prefix: spawnd/{run_id}/
  auto_cleanup: true
```

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SPAWND_LOG_LEVEL` | `info` | Log verbosity |
| `SPAWND_MAX_AGENTS` | `5` | Max concurrent agents |
| `SPAWND_MODEL` | `sonnet` | Default model |
| `SPAWND_RUN_ID` | - | Override run id for `spawnd run` |
| `SPAWND_RESUME` | `false` | Resume existing run when set |
| `SPAWND_TELEMETRY` | `true` | Enable or disable telemetry capture |
| `ANTHROPIC_API_KEY` | - | Required for SDK |

### First Run Setup

```bash
# After installation
spawnd init

# Creates:
# - ~/.claude/spawnd/config.yaml (default config)
# - Run-specific log/telemetry dirs created on first run
# - Validates Claude SDK is available
```

## Testing Strategy

### Test Pyramid

```
              ┌─────────────┐
              │  E2E Tests  │  ← Real agents, real git
              │   (few)     │
              ├─────────────┤
              │ Integration │  ← Mock SDK, real git
              │  (medium)   │
              ├─────────────┤
              │ Unit Tests  │  ← Pure functions, mocked deps
              │  (many)     │
              └─────────────┘
```

### Unit Tests

Test pure logic without external dependencies:

```python
# tests/unit/test_deps.py
def test_topological_sort():
    agents = [
        AgentConfig(name="a", depends_on=[]),
        AgentConfig(name="b", depends_on=["a"]),
        AgentConfig(name="c", depends_on=["a", "b"]),
    ]
    graph = DependencyGraph(agents)
    assert graph.topological_order() == ["a", "b", "c"]

def test_get_ready_agents():
    agents = [
        AgentConfig(name="a", depends_on=[]),
        AgentConfig(name="b", depends_on=["a"]),
    ]
    graph = DependencyGraph(agents)
    ready = graph.get_ready_agents(completed=set())
    assert [a.name for a in ready] == ["a"]

# tests/unit/test_parser.py
def test_parse_plan_spec():
    yaml_content = """
    name: test-plan
    agents:
      - name: foo
        prompt: "Do foo"
    """
    spec = parse_plan_spec(yaml_content)
    assert spec.name == "test-plan"
    assert len(spec.agents) == 1

# tests/unit/test_name_inference.py
def test_infer_name_from_prompt():
    assert infer_agent_name("Implement auth") == "auth"
    assert infer_agent_name("Fix the login bug") == "login"
    assert infer_agent_name("Refactor user service") == "user"
```

### Integration Tests (Mock SDK)

Test orchestration logic with mocked agents:

```python
# tests/integration/conftest.py
@pytest.fixture
def mock_sdk():
    """Mock Claude SDK client that completes via mark_complete tool."""
    class MockClient:
        def __init__(self):
            self.session_id = str(uuid4())

        async def connect(self, prompt):
            pass

        async def receive_response(self):
            yield TextBlock(text="Working on task...")
            yield ToolUseBlock(name="mark_complete", input={"summary": "Task done"})
            yield ResultMessage(
                result="Task complete",
                is_error=False,
                total_cost_usd=0.10,
            )

        async def interrupt(self):
            pass

        async def disconnect(self):
            pass

    return MockClient

# tests/integration/test_scheduler.py
@pytest.mark.asyncio
async def test_scheduler_parallel_execution(mock_sdk, tmp_path):
    """Test parallel agents complete in any order."""
    plan = PlanSpec(
        name="test",
        agents=[
            AgentConfig(name="a", prompt="Task A"),
            AgentConfig(name="b", prompt="Task B"),
        ],
    )

    # Create test database
    db = open_db(plan.run.id if plan.run else f"{plan.name}-test")

    with patch("spawnd.runtime.executors.claude.ClaudeSDKClient", mock_sdk):
        scheduler = Scheduler(db, plan)
        result = await scheduler.run()

    assert result.completed == {"a", "b"}
    assert result.failed == set()

@pytest.mark.asyncio
async def test_scheduler_dependency_order(mock_sdk, tmp_path):
    """Test deps are respected."""
    plan = PlanSpec(
        name="test",
        agents=[
            AgentConfig(name="a", prompt="Task A"),
            AgentConfig(name="b", prompt="Task B", depends_on=["a"]),
        ],
    )

    order = []

    class TrackingMockClient(mock_sdk):
        def __init__(self, name):
            super().__init__()
            self.name = name

        async def connect(self, prompt):
            order.append(self.name)

    db = open_db(f"{plan.name}-test")

    with patch("spawnd.runtime.executors.claude.ClaudeSDKClient", TrackingMockClient):
        scheduler = Scheduler(db, plan)
        await scheduler.run()

    assert order.index("a") < order.index("b")
```

### Git Integration Tests

Test worktree and merge operations:

```python
# tests/integration/test_git.py
@pytest.fixture
def git_repo(tmp_path):
    """Create a git repo for testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo)
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=repo)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo)
    return repo

def test_create_worktree(git_repo):
    run_id = "test-run"
    worktree_path = create_worktree(run_id, "test-agent", repo_path=git_repo)
    assert worktree_path.exists()
    assert (worktree_path / ".git").exists()

def test_merge_branches(git_repo):
    # Create two worktrees with changes
    run_id = "test-run"
    wt1 = create_worktree(run_id, "agent-1", repo_path=git_repo)
    wt2 = create_worktree(run_id, "agent-2", repo_path=git_repo)

    (wt1 / "file1.txt").write_text("content 1")
    subprocess.run(["git", "add", "."], cwd=wt1)
    subprocess.run(["git", "commit", "-m", "Agent 1"], cwd=wt1)

    (wt2 / "file2.txt").write_text("content 2")
    subprocess.run(["git", "add", "."], cwd=wt2)
    subprocess.run(["git", "commit", "-m", "Agent 2"], cwd=wt2)

    # Merge both
    merge_branches(git_repo, [f"spawnd/{run_id}/agent-1", f"spawnd/{run_id}/agent-2"])

    # Verify both files exist on main
    assert (git_repo / "file1.txt").exists()
    assert (git_repo / "file2.txt").exists()
```

### E2E Tests (Real Agents)

Full system tests with real SDK (expensive, run sparingly):

```python
# tests/e2e/test_full_workflow.py
@pytest.mark.e2e
@pytest.mark.slow
async def test_full_parallel_workflow(git_repo):
    """Test complete workflow with real agents."""
    plan_content = """
    name: e2e-test
    agents:
      - name: hello
        prompt: "Create a file hello.py that prints 'Hello'"
        check: "python hello.py"
      - name: world
        prompt: "Create a file world.py that prints 'World'"
        check: "python world.py"
    on_complete: merge
    """

    plan_path = git_repo / ".spawnd/plans/test.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan_content)

    # Run spawnd
    result = subprocess.run(
        ["spawnd", "run", str(plan_path)],
        cwd=git_repo,
        capture_output=True,
        timeout=300,  # 5 min timeout
    )

    assert result.returncode == 0
    assert (git_repo / "hello.py").exists()
    assert (git_repo / "world.py").exists()
```

### Test Fixtures

```python
# tests/conftest.py
@pytest.fixture
def sample_plan_spec():
    return PlanSpec(
        name="test-plan",
        defaults=Defaults(max_iterations=10, on_failure="continue"),
        agents=[
            AgentConfig(name="a", prompt="Task A", check="true"),
            AgentConfig(name="b", prompt="Task B", depends_on=["a"]),
        ],
    )

@pytest.fixture
def agent_state():
    return AgentState(
        name="test",
        status="running",
        iteration=5,
        max_iterations=30,
        worktree=".spawnd/runs/test-run/worktrees/test",
        branch="spawnd/test-run/test",
    )
```

### CI Configuration

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"

      - name: Unit tests
        run: pytest tests/unit/ -v

      - name: Integration tests
        run: pytest tests/integration/ -v

  e2e:
    runs-on: ubuntu-latest
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4
      - name: E2E tests (requires API key)
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: pytest tests/e2e/ -v --timeout=600
```

## Implementation Phases

### Release Strategy

| Version | Phases | Key Features |
|---------|--------|--------------|
| **v0.1** | 1-3 | Core CLI, parallel execution, merge |
| **v0.2** | +5 | Event system, manager-worker coordination |
| **v0.3** | +4, +6 | Dashboard TUI, specialized roles |
| **v0.4** | +7 | Slash command, DX polish |

---

### Phase 1: CLI Foundation (v0.1)
**Goal**: Core CLI with single-agent execution

```
Files:
├── spawnd/
│   ├── __init__.py
│   ├── cli.py              # Click CLI: run, status, cancel, db
│   ├── models.py           # Pydantic: AgentConfig, PlanSpec
│   ├── db.py               # SQLite setup, migrations, queries
│   ├── executor.py         # SDK agent spawning (workers)
│   ├── tools.py            # Worker toolset (mark_complete, etc.)
│   └── git.py              # Worktree creation/cleanup
├── pyproject.toml
└── tests/
```

Tasks:
1. Click CLI skeleton with `spawnd run`, `spawnd status`, `spawnd cancel`, `spawnd db`
2. Pydantic models for AgentConfig, PlanSpec
3. SQLite database setup with WAL mode (.spawnd/runs/{run_id}/spawnd.db)
4. Git worktree creation (.spawnd/runs/{run_id}/worktrees/{name}/)
5. Worker toolset implementation (mark_complete, report_progress)
6. SDK agent spawning with standard toolset
7. Basic status table output (from DB query)

**Deliverable**: `spawnd run -p "auth: Impl auth"` spawns one background agent with tool-based completion

### Phase 2: Plan Specs & Parallel Execution (v0.1)
**Goal**: YAML plan specs, parallel agent coordination

```
Files:
├── spawnd/
│   ├── parser.py           # YAML plan spec parsing
│   ├── scheduler.py        # Parallel/sequential execution
│   └── deps.py             # Dependency resolution
```

Tasks:
1. YAML plan spec parser (including cost_budget)
2. Parallel agent spawning
3. Sequential (`--seq`) coordination
4. Dependency resolution for `depends_on`
5. Inline execution (`-p` flags)
6. Cost tracking and limits

**Deliverable**: `spawnd run plan.yaml` executes multi-agent pipeline with cost controls

### Phase 3: Merge & Logs (v0.1)
**Goal**: Branch consolidation, log viewing

```
Files:
├── spawnd/
│   ├── merge.py            # Git merge logic with fallback config
│   └── logs.py             # Log tailing
```

Tasks:
1. `spawnd merge` - consolidate completed branches
2. `spawnd logs {name}` - view agent logs
3. `spawnd logs {name} -f` - follow mode
4. Auto-cleanup worktrees on merge
5. Conflict detection with configurable fallback (spawn_resolver, manual, fail)
6. Manual conflict resolution mode

**Deliverable**: `spawnd merge` consolidates all completed work with conflict handling options

---

### Phase 4: Dashboard TUI (v0.3)
**Goal**: Rich terminal dashboard

```
Files:
├── spawnd/
│   └── dashboard.py        # Textual TUI
```

Tasks:
1. TUI with live agent status table (including cost)
2. Log panel with agent output
3. Keyboard controls (cancel, view logs)
4. Auto-refresh from state files

**Deliverable**: `spawnd dashboard` shows live status with controls

### Phase 5: Event System & Orchestration (v0.2)
**Goal**: Manager-worker coordination via events and blocking tools

```
Files:
├── spawnd/
│   ├── events.py           # Event queries and emission (uses DB)
│   ├── orchestration.py    # Circuit breaker, failure cascades
│   ├── manager_loop.py     # Manual loop for manager agents
│   ├── manager_tools.py    # Manager toolset
│   └── blocking.py         # Blocking tool implementation (DB polling)
```

Tasks:
1. Event emission and querying via SQLite (events table)
2. Event types: started, progress, clarification, blocker, done, error
3. Manual loop for manager agents (full context control)
4. Manager toolset (spawn_worker, respond_to_clarification, cancel_worker, etc.)
5. Blocking tools using responses table polling
6. Worker response polling/waiting mechanism
7. Circuit breaker implementation
8. Failure cascade handling

**Deliverable**: Managers receive worker events and can respond; workers can block for guidance

### Phase 6: Specialized Roles & Templates (v0.3)
**Goal**: Built-in roles and problem-type templates

```
Files:
├── spawnd/
│   ├── roles.py            # Role definitions and loading
│   ├── templates.py        # Template expansion
│   └── roles/              # Built-in role YAML files
│       ├── architect.yaml
│       ├── implementer.yaml
│       ├── tester.yaml
│       └── ...
├── templates/              # Built-in templates
│   ├── feature.yaml
│   ├── bugfix.yaml
│   └── refactor.yaml
```

Tasks:
1. Role definition schema and loading
2. Built-in roles: architect, implementer, tester, reviewer, debugger, refactorer, integrator
3. Custom role definition in plan specs
4. Template expansion logic
5. `--template` flag for CLI
6. Dynamic tree generation by architect agents

**Deliverable**: `spawnd run --template feature "Implement auth"` expands to full pipeline

### Phase 7: Slash Command & Polish (v0.4)
**Goal**: Claude Code integration, DX refinements

```
Files:
├── commands/
│   └── spawnd.md            # Single /spawnd slash command
```

Tasks:
1. `/spawnd` slash command (pass-through to CLI)
2. Pattern mode (`--each`)
3. Error handling improvements
4. Auto-merge on completion (`on_complete: merge`)
5. Per-agent failure modes (`on_failure: continue|stop|retry`)
6. `spawnd init` first-run setup

**Deliverable**: Full integration with Claude Code

## Key Differences from Original Plan

| Aspect | Original | Revised |
|--------|----------|---------|
| Primary interface | Slash commands | CLI (`spawnd`) |
| Slash commands | 7 separate commands | 1 pass-through (`/spawnd`) |
| Input format | Inline prompts | Plan spec YAML files |
| Agent naming | Explicit `--name` | Inferred from prompt |
| Loop mode | Hook-based, in-session | Eliminated (background only) |
| Completion signal | `<done/>` text marker | `mark_complete()` tool via MCP |
| Execution model | SDK subagents | `query()` tasks (workers) + `ClaudeSDKClient` (managers) |
| Worker coordination | Text markers, polling | In-process MCP tools with DB polling |
| State storage | JSON files | SQLite database (.spawnd/runs/{run_id}/spawnd.db) |
| Worktrees | `./worktrees/` (visible) | `.spawnd/runs/{run_id}/worktrees/` (hidden) |
| Branches | `{name}` | `spawnd/{run_id}/{name}` (namespaced) |
| Progress | Polling /status | Dashboard TUI |
| Two-system arch | Loop vs Orchestration | Single system (async tasks + MCP tools) |
| Target user | Human developer | Human + other agents |
| Cost controls | None | Per-agent + per-plan limits |
| Hierarchy depth | Unlimited | Up to 10 levels (validated at spawn) |
| SDK subagents | Used for workers | Not used (max 2 levels, blocks, Claude-decided) |

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent naming | Infer from prompt | "Implement auth" → `auth`, better DX |
| Run identity | Generated `run_id` per run | Namespaces state + enables resume |
| Worktree location | `.spawnd/runs/{run_id}/worktrees/` | Hidden, cleaner project root |
| Branch naming | Prefixed `spawnd/{run_id}/{name}` | Namespaced, avoids conflicts |
| Cleanup policy | Auto on merge | Less manual work, clean state |
| Worker execution | Async `query()` tasks | True parallelism, no SDK subagent limits |
| Manager execution | `ClaudeSDKClient` | Stateful multi-turn with event injection |
| Coordination tools | In-process MCP | Direct DB access, asyncio blocking |
| Hierarchy limit | 10 levels | Practical for automation, prevents runaway |

### Name Inference Algorithm

```python
def infer_agent_name(prompt: str) -> str:
    """Extract key term from prompt for agent name."""
    # Common patterns: "Implement X", "Add X", "Fix X", "Refactor X"
    patterns = [
        r"(?:implement|add|create|build)\s+(\w+)",
        r"(?:fix|resolve|debug)\s+(\w+)",
        r"(?:refactor|update|improve)\s+(\w+)",
    ]
    for pattern in patterns:
        if match := re.search(pattern, prompt, re.I):
            return match.group(1).lower()

    # Fallback: first significant word
    words = [w for w in prompt.split() if len(w) > 3]
    return words[0].lower() if words else f"task-{uuid4().hex[:6]}"
```

### File Layout

```
PROJECT/
├── .spawnd/                      # All spawnd state (per-project)
│   ├── plans/                   # User-created plan specs (YAML)
│   │   └── feature-auth.yaml
│   └── runs/
│       └── {run_id}/
│           ├── plan.yaml         # Snapshot of plan used for resume
│           ├── spawnd.db          # SQLite database (WAL mode)
│           ├── spawnd.db-wal      # WAL file (auto-managed)
│           ├── spawnd.db-shm      # Shared memory (auto-managed)
│           ├── worktrees/        # Agent worktrees (git ignored)
│           │   ├── auth/
│           │   └── cache/
│           ├── logs/             # Per-agent logs (files, not DB)
│           │   ├── auth.log
│           │   └── cache.log
│           └── telemetry/        # Prompt/tool traces (jsonl)
│               ├── prompts.jsonl
│               ├── tool_calls.jsonl
│               └── costs.jsonl
├── .gitignore                   # Contains: .spawnd/runs/
└── ...

~/.claude/spawnd/                 # Global config (shared across projects)
└── config.yaml                  # User defaults
```

**Why logs stay as files:**
- `tail -f` works natively
- Can grow large without bloating DB
- Easy to grep/stream
- Append-only access pattern suits files

### Branch Flow

```
main
 │
 ├── spawnd/{run_id}/auth     (agent: auth)
 ├── spawnd/{run_id}/cache    (agent: cache)
 └── spawnd/{run_id}/logging  (agent: logging)

After merge:
main ← spawnd/{run_id}/auth ← spawnd/{run_id}/cache ← spawnd/{run_id}/logging
       (deleted)    (deleted)     (deleted)
```

## Implementation Gaps

Status as of 2025-01-18. Features documented above but not yet fully implemented.

### Critical (Breaking or Non-functional)

| Feature | Location | Issue | Priority |
|---------|----------|-------|----------|
| ~~Merge conflict resolver~~ | ~~`models.py:54-57`, `merge.py`~~ | ~~`spawn_resolver` action defined but never invoked~~ | ~~P1~~ FIXED |
| ~~Plan completion status~~ | ~~`scheduler.py:406-514`~~ | ~~Plan never set to completed/failed on success path~~ | ~~P1~~ FIXED |
| ~~Cancel command~~ | ~~`cli.py:209-233`~~ | ~~Updates DB only, doesn't stop running processes~~ | ~~P1~~ FIXED |
| ~~`interactive_merge()`~~ | ~~`merge.py:162-189`~~ | ~~Calls undefined `merge_branch()` - NameError~~ | ~~P1~~ FIXED |
| Manager subprocess tools | `executor.py:262-340` | Text marker parsing instead of MCP tool calls | P2 (deferred - SDK mode recommended) |

### Partial Implementation

| Feature | Location | Issue |
|---------|----------|-------|
| ~~Cost budget `on_exceed`~~ | ~~`scheduler.py:231-253`~~ | ~~Only `pause` works; `cancel` and `warn` not implemented~~ FIXED |
| ~~Circuit breaker `pause`~~ | ~~`scheduler.py:326-365`~~ | ~~Only `cancel_all` and `notify_only` implemented~~ FIXED (was already implemented) |
| ~~Dependency context `paths` mode~~ | ~~`git.py:244-247`~~ | ~~Comment says filter, code does full merge~~ FIXED |
| ~~Resume worktrees~~ | ~~`scheduler.py:144-170`~~ | ~~Re-creates worktrees instead of reusing existing~~ FIXED |
| ~~Shared context injection~~ | ~~`scheduler.py:180-184`~~ | ~~Loaded then dropped, never passed to agents~~ FIXED |
| ~~Per-agent `max_cost_usd`~~ | ~~`executor_sdk.py`~~ | ~~Field exists but never enforced~~ FIXED (SDK mode; subprocess has cost=0 limitation) |

### Unused/Dead Code

| Item | Location | Notes |
|------|----------|-------|
| `ManagerSettings` model | `models.py:77-82` | Never instantiated or applied |
| `PlanSpec.run` field | `models.py` | Defined but never used |
| `MAX_HIERARCHY_DEPTH` | `parser.py:15-18` | Constant defined, never checked |
| `Milestone` model | `models.py:70-75` | Defined but never validated/tracked/displayed |
| `PlanSpec.on_complete` | `models.py` | Merge config ignored (merge is manual only) |

### Missing from Phases

**Phase 4: Dashboard TUI (v0.3)**
- Current: Basic polling loop in `cli.py:305-358`
- Missing: Rich panels, agent status, cost tracking, controls

**Phase 5: Event System (v0.2)**
- Missing: `events.py` module - coordination is ad-hoc in scheduler
- Missing: `manager_loop.py` - manager orchestration
- Missing: `blocking.py` - clarification blocking primitives

**Phase 6: Templates (v0.3)**
- Missing: `templates.py` module
- Missing: `--template` CLI flag
- Missing: Template YAML files
- Current: Roles hard-coded in `spawnd/roles.py`

**Phase 7: Slash Commands (v0.4)**
- Missing: Plugin integration
- Missing: `/spawnd` pass-through command

### Test Coverage Gaps

**No tests exist for:**
- ~~Scheduler execution flow~~ ADDED (test_scheduler.py - 13 tests)
- ~~Executor (subprocess and SDK)~~ ADDED (test_executor.py - 6 tests)
- ~~CLI commands (run/status/cancel/merge/dashboard)~~ ADDED (test_cli.py - 21 tests)
- ~~Git worktree management~~ ADDED (test_git.py - 7 tests)
- Coordination tools (mark_complete, request_clarification, etc.)
- ~~Merge helpers~~ ADDED (test_merge.py - 8 tests)
- Retry/stop failure handling
- ~~Circuit breaker behavior~~ ADDED (test_scheduler.py)
- ~~Cost budget enforcement~~ ADDED (test_scheduler.py)
- Resume handling
- Manager-worker clarification flows

**Current coverage (85 tests total):**
- `test_parser.py` - Plan YAML parsing
- `test_deps.py` - Dependency graph
- `test_db.py` - SQLite operations
- `test_roles.py` - Role templates
- `test_scheduler.py` - Scheduler init, result building, circuit breaker, cost budget (13 tests)
- `test_executor.py` - AgentConfig, system prompt building, mock worker (6 tests)
- `test_git.py` - Worktree create/reuse/remove, merge operations (7 tests)
- `test_cli.py` - CLI commands (run, status, cancel, logs, merge, clean, db, roles) (21 tests)
- `test_merge.py` - Merge order, conflict detection, squash merge (8 tests)

### SDK vs Subprocess Parity

| Feature | SDK (`executor_sdk.py`) | Subprocess (`executor.py`) |
|---------|------------------------|---------------------------|
| Coordination tools | MCP tools work correctly | Text marker parsing (fragile) |
| Cost tracking | Captured from ResultMessage | Not captured |
| Check failure handling | Sets failed + error event | Same |
| Env injection | `env=` param + os.environ | os.environ only |
| Manager loop | Multi-turn with ClaudeSDKClient | Single-shot with event injection |

### Recommended Fix Order

1. **P1 Critical** - Fix broken functionality
   - ~~`interactive_merge()` NameError~~ FIXED
   - ~~Plan completion status~~ FIXED
   - ~~Cancel command process termination~~ FIXED

2. **P2 Core** - Complete partial implementations
   - ~~Cost budget actions (cancel/warn)~~ FIXED
   - ~~Circuit breaker pause~~ FIXED (was already implemented)
   - ~~Shared context injection~~ FIXED

3. **P3 Tests** - Add test coverage for scheduler/executor
   - ~~Scheduler tests~~ ADDED (13 tests)

4. **P4 Features** - Implement missing phases
   - Dashboard TUI
   - Event system
   - Templates
