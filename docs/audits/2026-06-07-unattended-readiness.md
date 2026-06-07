# Unattended Workflow Readiness Audit

- **Date:** 2026-06-07
- **Question:** What is remaining in spawnd today for running real unattended workflows in real codebases?
- **Audited at:** `aeb5d67` (Fix contributor PR provenance lookup)
- **Re-verified at:** `572790f` (Merge PR #6, spawnd/automation-hardening)
- **Method:** Eight parallel gap-finder reviews, one per unattended-operation dimension
  (trigger, resilience, delivery, security, operations, realrepo, runtime, promise).
  Every claimed gap was independently adversarially verified against the code before
  inclusion; 54 raw findings deduplicated to 42 unique gaps, 2 claims refuted. After
  PR #6 merged, all 42 gaps were re-verified one-by-one against `572790f`, plus a full
  PR-diff sweep for unmapped behavioral changes.

Severity definitions, relative to the goal of unattended workflows on real codebases:

- **blocker** — unattended operation cannot work at all, or produces no usable outcome.
- **major** — works in a demo but is unsafe, unreliable, or operationally untenable
  unattended (silent data loss, stuck runs, security hole, no deploy path).
- **minor** — friction or polish; workarounds exist.

Gap ids (`G00`–`G53`) are stable references; numbering has holes where duplicate
findings were merged into one gap.

## Verdict

The deployed substrate is genuinely done: transactional claims with leases, the full
worker flow (claim, worktree, setup, runtime via `RuntimeObserver`, check, artifacts,
usage, provenance, dependent enqueue), three real runtimes, every promised CLI/HTTP/
Python interface wired to Postgres/Redis/S3, redaction enforced on artifacts, and
Alembic in place. What is missing is everything that turns one supervised demo run
into unattended operation: **the loop never closes** (output is never pushed, failures
are never retried, no triggers exist), **nothing is bounded** (no runtime timeouts, no
run-level budget enforcement), and **the trust model assumes a human is watching**
(no API auth, `bypassPermissions`, ambient credentials).

## Status after PR #6 (2026-06-07)

| Status | Count |
| --- | --- |
| Resolved | 0 |
| Partially addressed | 2 (G05, G32) |
| Open | 40 (including all 3 blockers) |

## Priority path

The shortest sequence to "actually unattended":

1. **Close the output loop** — commit and push agent branches; fix `pr create` (G01, G13).
2. **Close the recovery loop** — retryable error classification plus auto-retry in
   `fail_agent`, periodic lease reaping inside the worker loop, failure-policy
   enforcement: `stop`/`continue`/terminal runs (G02, G06, G07, G08, G09).
3. **Bound runaways** — wall-clock timeouts on setup/runtime/check, run-level budget
   and circuit-breaker enforcement, cancellation that kills subprocesses (G00, G10, G24, G34).
4. **Unpin workers from hand-provisioned hosts** — clone-on-demand from `source_repo`
   with credential injection (G05, G20).
5. **Fix the trust model** — API auth, secret refs instead of plaintext `agent.env`
   in `runs.spec` (G15, G17, G18, G19).
6. **Make it operable** — deploy artifacts, crash-tolerant poll loop, failure
   notifications, worktree cleanup (G14, G21, G22, G23).

Triggers (cron, webhooks, templates: G04, G41, G42) only matter after 1–4: a triggered
run today would still stall on the first transient error and deliver its output to
nowhere.

## What already holds up

Per-dimension verification of what the codebase genuinely covers (at `aeb5d67`,
strengthened by PR #6 where noted):

**Unattended initiation (`trigger`).** A run can be initiated programmatically without the interactive CLI: the Python API (spawnd/api.py: submit/run/pipeline/handoff) and the HTTP endpoint POST /runs (spawnd/server.py:58) both call submit_plan, so an external system that already decided "run now" can enqueue work over HTTP/Python. Plans can be parameterized in-process before submit (api.agent kwargs; plan_builder.expand_pattern_agents globs files into per-file agents at authoring time). reconcile (cli.py:221) and resume (cli.py:406) allow programmatic re-injection of already-persisted runs.

**Failure handling and recovery (`resilience`).** Postgres-transactional claim with row lock and lease token (repository.claim_agent, repository.py:245-323), per-agent attempts table, an idempotent lease-expiry sweep (expire_stale_leases, repository.py:868-929) that requeues retryable agents and marks attempts 'expired', a reconcile entry point that re-derives Redis hints from Postgres (worker.py:561-573, exposed via `spawnd reconcile` and POST /workers/reconcile), an outbox pattern around enqueue, in-process lease renewal with cancel-checking (worker.py:404-426), and explicit handling of runtime exceptions/CancelledError that records errors + transitions the agent. Setup subprocess honors a configurable timeout (worktrees.py:102).

**Where the work product lands (`delivery`).** The worker records rich git provenance per agent: it computes both committed (merge-base..HEAD) and uncommitted (diff HEAD) changes, stores a redacted patch artifact, captures branch/head/base SHAs, remote URL, commit message, and diff numstats into the durable git_provenance table (spawnd/workers/worker.py:476-532). Dependent agents can consume an upstream agent's work via setup_worktree_with_deps, which supports full-merge, diff_only, and paths modes with rename/delete handling (spawnd/gitops/worktrees.py:224-299), and this IS wired into the deployed worker path (worker.py:201-211). The patch artifact is therefore not the only durable record of what changed — provenance metadata is queryable via 'spawnd provenance'.

**Secrets, auth, and isolation (`security`).** Codex executor has a genuine, configurable sandbox/approval boundary (codex.py:97-118, 143-148): SPAWND_CODEX_SANDBOX defaults to workspace-write and approval defaults to deny_all, with danger-full-access gated behind an explicit env flag. Redaction helpers exist and are wired through the artifact store (store.py:109, worker.py:536-543): setup/runtime/check/final outputs and patches are redacted unless orchestration.artifacts.capture_raw is true, and the agents table stores only redact_env() metadata + hashes/previews of prompts and check commands (repository.py:81-91). Secret-key detection covers token/secret/password/credential/auth/api_key/private_key patterns in attributes and KEY=value assignments (redaction.py:10-17).

**Deploy and operate (`operations`).** Schema deployment has a real story: Alembic is wired (alembic.ini, spawnd/migrations/env.py reads SPAWND_DATABASE_URL, README documents `alembic upgrade head`), and the worker correctly owns durable state transitions. Per-agent cost caps ARE enforced inside executors (claude.py:101, openai.py:81 raise cost_exceeded). Lease renewal, cancellation propagation, and queue-outbox reconciliation exist (worker.py:404, reconcile_ready_agents). OTEL trace export is configurable and mirrored to Postgres (telemetry.py). CLI exposes broad inspection (status/events/logs/checks/trace/provenance).

**Real-codebase mechanics (`realrepo`).** Per-agent worktrees are created with isolated branches (spawnd/<run>/<agent>), base_ref/fetch are configurable via WorktreeSource (specs.py:44-47, worker.py:191-200), a per-worktree setup command with timeout and env injection exists and is recorded as an invocation (worker.py:220-262, worktrees.py:89-106), the check command runs in the worktree with inherited host env (worker.py:438), dependency context is merged into worktrees in full/diff_only/paths modes (worktrees.py:224-299), and git provenance (head/base/merge-base/patch) is captured per agent (worker.py:476-532).

**Provider runtime maturity (`runtime`).** All three runtimes (claude SDK, codex SDK+CLI, openai-agents) actually execute and emit RuntimeObserver facts (event/invocation/usage/final/error) that the PostgresRuntimeObserver flushes into deployed state (spawnd/runtime/observer.py:129-170). The worker dispatches per-agent via claimed.runtime stored at claim time (spawnd/state/repository.py:295,318; spawnd/workers/worker.py:120,313). Claude runs fully unattended (permission_mode="bypassPermissions" when write_allowed, spawnd/runtime/executors/claude.py:72). Codex CLI observability is honest per CLAUDE.md: it emits a single codex_cli_subprocess invocation with argv preview, returncode, and byte counts and does not invent internal tool traces (spawnd/runtime/executors/codex.py:343-351). Missing provider SDKs are handled with clean error messages rather than crashes (claude.py:44-47, codex.py:90-94/226-229, openai.py:40-43). Claude/OpenAI enforce max_iterations as max_turns and check per-agent max_cost_usd.

**Promise vs reality (`promise`).** The full deployed interface surface promised by PLAN.md/README.md is genuinely present and wired to the Postgres/Redis/S3 path, not legacy/in-memory. Every CLI command (run, submit, worker --once/--poll, reconcile, worker-heartbeat, status, events, live-events, artifacts, logs, checks, trace, provenance, cancel, resume, pr create, serve, roles) routes through DeployedRepository + RedisCoordinator + S3ArtifactStore (spawnd/cli.py). All HTTP endpoints in README exist in spawnd/server.py and delegate to the same submit/repository functions. The Python API (agent/run/submit/pipeline/handoff/status/events/...) in spawnd/api.py uses submit_plan and DeployedRepository. The worker execution flow (claim->worktree->setup->runtime via RuntimeObserver->check->artifacts->usage->provenance->enqueue dependents) is implemented end-to-end in spawnd/workers/worker.py. The three real runtimes (claude SDK, codex SDK+CLI, openai agents) actually execute (spawnd/runtime/executors/). Alembic migration 0001 + env.py correctly build schema from spawnd/state/schema.py; S3ArtifactStore is a real boto3 implementation. Cancellation reaches a running agent via the lease-renewal is_cancelled check (worker.py:408). Lease expiry, transactional claims, and Postgres-driven reconcile are real. Legacy swarm remnants are largely gone; only spawnd/core/deps.py is dead (self-referenced + one test, no deployed consumer) and is harmless.

## Refuted claims

Two findings were raised during the audit and refuted by adversarial verification —
recorded so they are not re-reported:

- **(delivery) "Patch artifact mixes committed and uncommitted diffs into one non-applicable blob."**
  The narrow observation is accurate (`worker.py` joins `git diff <merge_base>..HEAD` and
  `git diff HEAD`, which is not one cleanly `git apply`-able patch when the same file appears
  in both), but the claim's load-bearing premise — that the patch artifact is the designated
  recovery/landing mechanism — is false; the branch plus provenance rows are the recovery
  path, and the artifact is evidence. Kept as context for G01/G13 rather than a gap.
- **(realrepo) "Agent env passed to Claude SDK replaces host env, so the worktree venv/PATH is invisible."**
  False conclusion. The spawnd-side reading was correct (`ClaudeAgentOptions(env=...)` is
  built without copying `os.environ`), but the installed `claude_agent_sdk` merges the
  provided env over the inherited process environment when spawning the CLI, so PATH and
  the worktree venv remain visible.

## PR #6 re-verification detail

PR #6 (`aeb5d67..572790f`, "automation-hardening": *Harden deployed automation
execution*, *Release worker ownership on cancellation*, *Protect runtime execution
context env*, *Redact deployed connection strings in artifacts*) is real state-hygiene
and safety hardening, but orthogonal to the blockers. Behavioral changes found by the
full-diff sweep:

- Per-run source-repo resolution: the worker now resolves the git source per claimed run from run['source_repo'] (falling back to the worker's source_path) and validates it is an existing git repo via `git rev-parse --show-toplevel`, instead of pinning every run to the worker's single source_path. A new frozen RunSource(repo_path, base_ref, fetch) carries this through worktree creation, setup, and provenance. base_ref precedence is: plan.orchestration.worktree_source.base_ref (explicit override) else run['source_ref'].
  - Where: spawnd/workers/worker.py:240-273 (_resolve_run_source, RunSource at 39-46), wired at worker.py:131, 157-178, 200, 209, 251
  - Concern: This is a behavioral broadening: one worker can now service runs targeting different repos. fetch defaults to False whenever orchestration.worktree_source is absent even if the run supplied a source_ref, so an unfetched local repo missing that ref will fail at worktree creation (handled as a worktree_create failure, not silent). Lower-risk but worth noting for unattended ops where the ref lives only on the remote.
- New worktree-source and worktree-create failure paths fail the claimed agent cleanly: each catches exceptions, stores a redacted 'source-error'/'worktree-error' artifact, records a runtime_error (source 'worktree_source' / 'worktree_create'), and calls fail_agent with the attempt_id. Previously a worktree failure had no dedicated handling.
  - Where: spawnd/workers/worker.py:130-156 (source) and 158-178 (worktree)
- AgentConfig.execution_env() centralizes runtime env construction for all three providers and makes the four SPAWND identity vars (SPAWND_RUN_ID/AGENT_NAME/PARENT_AGENT/TREE_PATH) authoritative by applying them LAST, after base and config.env. Previously claude/openai applied SPAWND vars first then let config.env override them, and codex applied os.environ + SPAWND + config.env (config.env winning). Now agent env can no longer spoof those four identity vars. Provider os.environ inheritance is preserved: codex passes os.environ as base, claude/openai pass no base (env-isolated as before).
  - Where: spawnd/runtime/agent_run.py:30-44; callers spawnd/runtime/executors/claude.py:55, openai.py:47, codex.py:206 (codex passes os.environ)
  - Concern: Env protection is narrowly scoped to the four identity vars only. Legitimate tuning vars like SPAWND_CODEX_MODEL / SPAWND_CODEX_ENGINE (read in codex.py:50,59) are NOT in the protected set, so they still flow through from config.env/os.environ. No legitimate env passing is broken.
- Redaction now scans every UPPERCASE-style KEY=VALUE assignment, redacts the value only when the key matches SENSITIVE_KEY (which now also includes database_url/redis_url), preserves non-secret assignments verbatim via match.group(0), and supports quoted values. This catches lowercase/embedded secret keys (e.g. ?api_key=... in a URL) and deployed connection-string env (SPAWND_DATABASE_URL/SPAWND_REDIS_URL) that the old uppercase-anchored ASSIGNMENT_SECRET regex missed.
  - Where: spawnd/artifacts/redaction.py:14-17 (ASSIGNMENT), 37-42 (redact_freeform_text)
  - Concern: Bare (unquoted) values still use [^\s]+, so a secret value with no surrounding space greedily swallows trailing non-space text on the same token (e.g. 'TOKEN=abc;echo done' -> 'TOKEN=<redacted> done', losing ';echo'). This over-greedy capture predates the PR (old regex had the same value pattern) and only affects artifact readability for secret lines, not correctness; non-secret lines are preserved exactly. Not a regression.
- cancel_agent now also clears worker_id and heartbeat_at, marks the running agent_attempts row cancelled (finished_at set), and calls refresh_run_status after the transaction. cancel_run now short-circuits if the run is already terminal (returns 0), records cancelled_at, clears worker_id/heartbeat_at on agents, and cancels running attempts.
  - Where: spawnd/state/repository.py:206-232 (cancel_agent), 443-486 (cancel_run)
  - Concern: Reviewed the double-execute risk: no double-execution path. Cancellation sets status='cancelled' and nulls leased_until; claim_agent requires status='queued' (repository.py:277,294) and expire_stale_leases selects only status='running' (repository.py:934), so a cancelled agent is never re-claimable or lease-expirable. refresh_run_status short-circuits on a cancelled run (repository.py:496-497) so it cannot flip a cancelled run back to running/queued. cancel_agent is idempotent (terminal-status guard at line 204), so the worker's CancelledError handler calling cancel_agent after an external cancel_run is safe.
- claim_agent now sets runs.status='running' inside the claim transaction, so a run flips to running on first claim rather than relying solely on later refresh_run_status passes.
  - Where: spawnd/state/repository.py:332-336
  - Concern: Concurrency reviewed: the agent-status='queued' gate prevents a late claim from resurrecting a cancelled run (a cancelled run's agents are all cancelled, so no agent is claimable). Safe under both claim-then-cancel and cancel-then-claim orderings.
- fail_agent (and the claim path's release) now also clear heartbeat_at when releasing an agent, completing the ownership-release column set (worker_id/lease_token/leased_until/heartbeat_at).
  - Where: spawnd/state/repository.py:430 (fail_agent), 402 (complete_agent), 263 (claim release path)
- expire_stale_leases now collects affected run_ids and calls refresh_run_status for each after the transaction, so a run's aggregate status is recomputed (e.g. back to 'queued') after a retryable lease expiry. Previously the run status was left stale after lease expiry.
  - Where: spawnd/state/repository.py:929, 981, 984-985
  - Concern: refresh_run_status runs in its own transaction after the expiry transaction commits; a concurrent cancel could interleave, but refresh's cancelled short-circuit (line 496) makes that safe.
- Outbox/Redis publishing is unified via a new _publish_ready_agent helper used by both the worker's _enqueue_agent and reconcile_ready_agents. reconcile_ready_agents now records a queue_outbox row (and marks it published) for expired-lease requeues too, and de-dupes so an agent already published via the expired-lease pass is not enqueued again in the ready_agents pass.
  - Where: spawnd/workers/worker.py:289-291 (_enqueue_agent), 296-318 (reconcile_ready_agents), 321-324 (_publish_ready_agent)
  - Concern: De-dupe is per single reconcile call only; outbox rows are still appended on every reconcile pass for genuinely-ready agents (idempotent enqueue is fine since Redis entries are wakeups), so repeated reconciliation will accumulate published outbox rows over time. Functionally harmless but unbounded outbox growth is not pruned here.
- HTTP /runs submit body now forbids extra fields (ConfigDict(extra='forbid')), drops the server-side plan_file field, requires an inline serialized plan, and runs validate_plan at the edge, returning 422 with the structured error list on invalid plans (e.g. unknown depends_on). Previously the server would read a server-local plan_file path and did not validate the plan before submit_plan.
  - Where: spawnd/server.py:21-25 (SubmitBody), 60-66 (submit handler)
  - Concern: Removing plan_file is a server-API contract change but breaks no internal caller: the CLI submit path parses the YAML locally and calls submit_plan in-process (cli.py:157-166), and there is no in-repo HTTP client that posts plan_file. submit_plan itself does not validate (submission.py:23-24), so this is the only validation gate on the HTTP boundary — genuinely new protection, not duplicative. extra='forbid' makes any old client sending plan_file get a 422 rather than silent acceptance.
- record_artifact gained optional attempt_id/session_id/invocation_id columns and record_check gained attempt_id/runtime_invocation_id/shell/cwd_locator/env_metadata/started_at/completed_at columns; the worker now threads attempt_id (and where applicable session_id/invocation_id) into every artifact store and passes shell='/bin/sh', cwd_locator, and precise started_at/completed_at to record_check, wiring provenance that was previously left null. Schema columns already existed from the prior migration (schema.py); this PR only populates them.
  - Where: spawnd/state/repository.py:1075-1078 (record_artifact), 1115-1131 (record_check); worker.py _store_text_artifact 674-704 and all call sites, _run_check 573-586
  - Concern: record_check passes shell='/bin/sh' hardcoded, which matches subprocess.run(shell=True) behavior on POSIX but would be inaccurate on non-POSIX hosts; deployed targets are POSIX so low risk. The new env_metadata=redact_attributes(...) branch in record_check is dormant for the worker (worker never passes env_metadata), so no artifact-corruption risk on the current path.

## Gap register

Each entry records the gap as confirmed at `aeb5d67`, with evidence re-verified at
`572790f` after PR #6. Evidence citations use file:line at the commit indicated;
lines drift as the code evolves — treat the cited symbol, not the number, as the
anchor. `[…]` marks evidence clipped during extraction.

### G00 · No wall-clock timeout on runtime execution; Codex also lacks any turn limit

**Severity:** blocker · **Dimensions:** resilience, runtime · **Status:** Open — re-verified unchanged at 572790f

If a provider runtime hangs (network stall, stuck tool loop, frozen subprocess), the worker awaits it indefinitely. A worker processes one agent at a time (run_once), so one hung agent stalls the entire worker. Lease renewal keeps succeeding because the process is alive, so the lease never expires either — there is no path out without operator intervention.

**Evidence (at aeb5d67):**

- Codex CLI subprocess has no timeout: _run_subprocess calls subprocess.run with capture_output/text only, no timeout arg (codex.py:175-188). Codex SDK turn `await thread.run(...)` has no asyncio.wait_for (codex.py:309). _run_runtime (worker.py:306-313) awaits the executor with only _with_lease_renewal, which never imposes a deadline. grep for timeout/wait_for/signal/kill across spawnd/runtime/*.py: […]
- config.max_iterations is passed as max_turns ONLY for Claude (claude.py:71) and OpenAI (openai.py:68). grep 'max_turns|max_iterations|timeout' over spawnd/runtime/executors/codex.py returns nothing — neither _run_sdk (codex.py:280-330) nor _build_codex_command (codex.py:121-155) passes any turn/iteration bound. The CLI subprocess uses subprocess.run with NO timeout= (codex.py:181-188). The worker […]

**Complete looks like:** Setup, runtime, and check each run under a configured wall-clock deadline; on expiry the subprocess/turn is killed and the agent transitions to timeout.


### G01 · Worker branches are never pushed to a remote; 'pr create' cannot work unattended

**Severity:** blocker · **Dimensions:** delivery, promise · **Status:** Open — re-verified unchanged at 572790f

Completed agent work lives on a local branch in the worker's scratch worktree (SPAWND_SCRATCH_ROOT). Nothing ever pushes that branch to origin. `spawnd pr create` invokes `gh pr create --head <branch>` with no preceding push or fetch, so for any real remote the PR creation aborts. There is no path that gets the agent's code off the worker host.

**Evidence (at aeb5d67):**

- No 'git push' anywhere in spawnd (grep over spawnd/ for push/clone returns only worktrees.py:482 'remote get-url origin' read-only and worktrees.py:75 'fetch'). 'pr create' (cli.py:448-470) runs `gh pr create --head <branch>` against branch `spawnd/<run>/<agent>` that exists only in the worker's local scratch worktree (worktrees.py:69). gh requires the head branch to exist on the remote, so the co […]
- No git push anywhere: grep -rn for push over spawnd/ returns zero matches. Worktrees and branches live only in worker-local scratch (spawnd/gitops/worktrees.py:21 SPAWND_SCRATCH_ROOT or repo/.spawnd-scratch). _record_git_provenance (spawnd/workers/worker.py:476-532) records branch/head/patch but does not push. spawnd/cli.py:462-466 'pr create' runs `gh pr create --head <branch>` from the orchestra […]

**Complete looks like:** The worker (or pr create) pushes each agent branch to a configured remote after completion, then `gh pr create` opens a PR against the pushed branch; credentials/remote are configurable.


### G02 · Provider errors never classified retryable + no auto-retry: transient rate-limit/auth/overload permanently fails the agent and stalls the chain

**Severity:** blocker · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

Anthropic 429/529-overloaded, OpenAI rate limits, and transient network blips are indistinguishable from genuine fatal errors. Any single provider hiccup terminally fails the agent; dependents never enqueue, and the run sits stuck until a human runs 'spawnd resume'. That defeats unattended operation on real codebases where multi-hour runs routinely hit at least one transient provider error.

**Evidence (at aeb5d67):**

- No executor ever passes retryable: grep 'retryable' over spawnd/runtime returns only observer.py:168 (reads data that is never set). claude.py:123, codex.py:243/277, openai.py:105 all call observer.error(...) with no retryable flag. The worker's failure path (spawnd/workers/worker.py:331-346) calls record_runtime_error with no retryable and then fail_agent. fail_agent (spawnd/state/repository.py:3 […]

**Complete looks like:** Executors classify provider errors (rate limit, overload, auth, network) and mark transient ones retryable; the worker auto-requeues an agent for another attempt (respecting retry_count) on retryable failure without manual resume.


### G04 · No webhook / repo-event receiver (push/PR/issue triggers)

**Severity:** major · **Dimensions:** trigger · **Status:** Open — re-verified unchanged at 572790f

A real unattended workflow on a real codebase is normally driven by VCS events (push to branch, PR opened, issue labeled). Nothing in spawnd receives such events or turns them into a submitted run, so it cannot react to repository activity.

**Evidence (at aeb5d67):**

- spawnd/server.py:58-110 defines only /runs (POST), /runs/{id}* (GET), cancel, resume, /workers/reconcile. absent: grep for 'webhook|pull_request|on_push|github.event|x-hub-signature|payload.*ref' across spawnd/ finds only _pull_request_for_branch (worker.py:610, an OUTBOUND gh-CLI PR lookup, not an inbound trigger). No HMAC/signature verification, no event-to-plan mapping.

**Complete looks like:** An authenticated inbound endpoint validates a provider event (e.g. GitHub push/PR), maps it to a plan template parameterized with the event's repo/ref/PR number, and submits a run.


### G05 · Worker ignores submitted source_repo; no clone-on-demand/remote-URL support

**Severity:** major · **Dimensions:** delivery, promise, realrepo, trigger · **Status:** Partially addressed by PR #6

Even if an external event triggered a submit naming a repo/ref, the worker never acquires that repo. It only operates on whatever git checkout already exists at its configured --source-path. So unattended, event-driven runs against an arbitrary 'real codebase' cannot work without a human pre-placing the correct repo on the worker host.

**Evidence (at aeb5d67):**

- submit_plan persists source_repo/source_ref (submission.py:18-24). But DeployedWorker.__init__ pins execution to its own local path: self.source_path = (source_path or Path.cwd()).resolve() (spawnd/workers/worker.py:59); _execute_claimed reads run via get_run (worker.py:98) only for the plan spec, then runs under _pushd(self.source_path) and create_worktree(repo_path=self.source_path) (worker.py:1 […]
- DeployedWorker.source_path defaults to Path.cwd() (worker.py:59); _prepare_worktree calls create_worktree(repo_path=self.source_path) (worker.py:194-200). submit's --source-repo is only stored as metadata (submission.py:18-24, repository.py:51-66) and never used to clone. No 'clone' call exists in spawnd/ (grep). source_ref is read for provenance base only (worker.py:478).
- spawnd/gitops/worktrees.py:67-79 create_worktree only runs `git worktree add` against repo_path=self.source_path; spawnd/workers/worker.py:59 source_path=(source_path or Path.cwd()); WorktreeSource has only base_ref+fetch (spawnd/models/specs.py:44-47). source_repo is stored as run metadata (spawnd/state/submission.py:18-24, schema.py:52) but the worker never reads run['source_repo'] to locate/clo […]
- spawnd/state/submission.py:24 stores source_repo on the run; spawnd/state/repository.py:51,65 persist it. But spawnd/workers/worker.py:59 sets self.source_path = (source_path or Path.cwd()) and _prepare_worktree (worker.py:191-200) passes repo_path=self.source_path to create_worktree. The worker only reads run.get('source_ref') for provenance base (worker.py:478) and NEVER reads run['source_repo']

**Complete looks like:** The worker clones/fetches the run's recorded source_repo at source_ref into worker scratch and runs the worktree against that, so a triggered run fully specifies which codebase/revision it operates on.

**Post-merge detail (572790f):**

PR #6 partially addressed this. The worker now DOES read run['source_repo'] (the pre-merge "never reads it" evidence is stale). New _resolve_run_source at /Users/sour4bh/dev/spawnd/spawnd/workers/worker.py:247-266 reads run.get('source_repo'), and it is wired into the live deployed execution path: called at worker.py:131 inside _execute_claimed, result flows to _prepare_worktree (worker.py:159, which now passes repo_path=source.repo_path at worker.py:270-276), to _run_setup_if_needed (worker.py:200), and to _record_git_provenance (worker.py:206). source_ref still feeds base_ref (worker.py:261). So a submit naming a DIFFERENT repo that already exists locally now targets that repo instead of a hardcoded source_path.

BUT the gap's core ("clone/fetch on demand so a triggered run fully specifies which codebase it operates on") is NOT met. source_repo is treated strictly as a pre-existing local filesystem path: worker.py:252 does Path(str(source_repo)).expanduser(); worker.py:253-254 raises GitError('Run source repository does not exist') if the path is absent; worker.py:255-258 then requires it already be a git checkout via `git rev-parse --show-toplevel`. There is no clone-on-demand and no remote-URL support: grep across spawnd/ finds no `git clone`/`ls-remote`/`remote add`; the only urlparse is in artifacts/store.py (S3), and the only `git fetch --prune origin` is worktrees.py:75, which runs against an already-local repo's pre-existing origin remote. CLI still documents --source-repo as "Source repository path" (cli.py:125,154) and defaults it to str(Path.cwd()) at cli.py:103. Event-driven HTTP submit accepts source_repo (server.py:25,71) but it is persisted as-is (submission.py:24, repository.py:51,65) and only ever resolved as a local path.

Unattended failure mode that remains: an event-triggered submit (server.py) naming a repo/URL that is NOT already cloned on the worker host fails the agent rather than acquiring it. This is now codified as intended behavior in test_deployed_worker.py:258-289 (test_worker_fails_claimed_agent_when_source_repo_is_missing asserts source='worktree_source' error with 'does not exist'). A human must still pre-place the correct repo on every worker host for arbitrary-codebase runs.

Note: CLAUDE.md (updated alongside PR #6) now declares this as the contract — "A run's `source_repo` is the execution source of truth when present. It is a local git repository path reachable by the worker, not a remote clone request." Closing this gap therefore means changing that declared contract (adding a remote-acquisition path), not merely filling an omission.

**Caveats:** Could not execute the source-resolution tests: tests/test_deployed_worker.py source tests are skipped without SPAWND_TEST_DATABASE_URL (3 skipped). Wiring was verified by reading the live call chain, not by running the deployed path. Classification "partial" reflects that local-path source selection is now real and wired, but remote/clone acquisition (the documented "what complete looks like") is absent for all runtimes; severity stays major because the unattended event-driven-against-arbitrary-codebase scenario still cannot work without manual repo pre-placement.


### G06 · Nothing automatically reclaims agents when a worker process dies

**Severity:** major · **Dimensions:** resilience · **Status:** Open — re-verified unchanged at 572790f

If a worker is SIGKILLed/OOM-killed/crashes mid-agent, its lease-renewal coroutine dies with it. The Postgres agent row stays status='running' with a future leased_until until it passes AND some external actor calls expire_stale_leases. Unattended, no such actor exists, so the agent is stuck 'running' forever and the run never completes or fails on its own.

**Evidence (at aeb5d67):**

- expire_stale_leases (repository.py:868) and reconcile_ready_agents (worker.py:561) are only invoked by the manual `spawnd reconcile` CLI (cli.py:221-229) and POST /workers/reconcile (server.py:107-109). The worker poll loop (worker.py:81-88) only calls run_once->claim_next_agent; it never sweeps leases. Searched spawnd/ for cron/scheduler/interval/background/create_task scheduling of reconcile: on […]

**Complete looks like:** A worker (or a dedicated reaper) periodically runs expire_stale_leases + ready-agent reconciliation on an interval without an operator manually invoking the CLI/HTTP endpoint.


### G07 · Agents never retry automatically on failure (on_failure='retry'/retry_count inert)

**Severity:** major · **Dimensions:** promise, resilience · **Status:** Open — re-verified unchanged at 572790f

The configured retry policy is effectively inert for the common case (the agent failed, not the worker). Unattended, a transient failure (flaky check, provider 500) is permanent. retry_attempt is incremented only on lease expiry, not on application-level failure.

**Evidence (at aeb5d67):**

- fail_agent (repository.py:391-415) sets status='failed' and clears the lease but never re-queues, regardless of on_failure/retry_count. The retry_attempt<retry_count check exists only in expire_stale_leases (repository.py:887) and resume_run (repository.py:478). So a runtime/check failure with on_failure='retry', retry_count=3 stays 'failed' until a human runs `spawnd resume` (cli.py:404-419).
- fail_agent (spawnd/state/repository.py:391-415) unconditionally sets status='failed' with no retry-eligibility branch and no re-enqueue. Retry logic exists only in resume_run (repository.py:478, triggered by manual `spawnd resume`) and in lease-expiry reconcile (repository.py:887). The worker after fail_agent does nothing further (worker.py:118,253,345,380,473 all call fail_agent and return 'faile […]

**Complete looks like:** fail_agent (or the worker after a failed attempt) honors on_failure='retry'/retry_count by re-queuing the agent with backoff up to the limit, then marking it terminally failed.


### G08 · on_failure='stop' is never enforced; a failed agent does not stop the run

**Severity:** major · **Dimensions:** resilience · **Status:** Open — re-verified unchanged at 572790f

When a critical agent fails with on_failure='stop', the run should halt and stop spending on in-flight/queued siblings. Instead siblings keep running and dependents stay pending. The policy is silently ignored, so unattended runs over-spend and produce inconsistent partial results against the author's stated intent.

**Evidence (at aeb5d67):**

- on_failure values 'stop' and 'continue' are stored (schema.py:87, repository.py:87) but only the 'retry' value is ever read (repository.py:478, 887). grep for on_failure across workers/ and state/ shows no branch acting on 'stop'/'continue'. fail_agent (repository.py:391) does not cancel siblings or the run.

**Complete looks like:** A failed agent with on_failure='stop' transitions the run to a terminal failed state and cancels non-terminal siblings; 'continue' lets independent siblings proceed.


### G09 · Dependents of a failed agent stuck 'pending' forever; run never terminal; on_failure='continue' ineffective

**Severity:** major · **Dimensions:** promise, resilience · **Status:** Open — re-verified unchanged at 572790f

After an upstream agent fails, its downstream agents remain 'pending' indefinitely and the run is reported 'queued'/'running' forever. An unattended operator polling status sees a run that never finishes and never surfaces as failed, with no automatic resolution.

**Evidence (at aeb5d67):**

- mark_newly_ready_agents promotes a pending agent to queued only if all deps are in the 'completed' set (repository.py:225-243); a failed dep is never 'completed'. refresh_run_status (repository.py:435-467) only returns terminal ('completed'/'failed') when ALL agents are terminal; 'pending' is not in the terminal set (repository.py:438), so a run with a failed agent + pending dependents falls throu […]
- mark_newly_ready_agents (spawnd/state/repository.py:225-243) promotes a pending agent only when `all(dep in completed for dep in depends_on)`, where completed = names with status=='completed' (line 230). A dependency that ends 'failed' (even with on_failure='continue') is never in `completed`, so its dependents stay 'pending' indefinitely. on_failure is persisted (repository.py:87) but the schedul […]

**Complete looks like:** When a dependency fails terminally, dependents are marked blocked/cancelled (or skipped) and the run reaches a terminal status reflecting the failure.


### G10 · Cancellation does not reach the running provider subprocess; orphaned processes continue

**Severity:** major · **Dimensions:** resilience · **Status:** Open — re-verified unchanged at 572790f

Cancelling a run leaves any in-flight Codex CLI subprocess (and its tool-spawned children, e.g. the bash tool) running, continuing to mutate the worktree and burn cost after the agent is marked cancelled. Cancellation latency is also up to ~30s because it is only checked on the lease-renew tick.

**Evidence (at aeb5d67):**

- Cancel sets a Redis key (redis.py:90-95); the worker only observes it inside _renew_lease_until_done at interval max 1s..30s (worker.py:405-409) and calls task.cancel(). But the Codex CLI runs via asyncio.to_thread(subprocess.run,...) (codex.py:181) which cannot be interrupted by coroutine cancellation, and no SIGTERM/kill is sent (grep for signal/terminate/kill in runtime/*.py returns nothing). T […]

**Complete looks like:** Cancellation signals the live subprocess/turn (process-group SIGTERM/kill) promptly so no orphaned provider process survives a cancelled or expired agent.


### G12 · No integration / merge-to-main step; merge-order machinery is dead code

**Severity:** major · **Dimensions:** delivery · **Status:** Open — re-verified unchanged at 572790f

Each agent produces an isolated branch/patch. Nothing combines parallel agents' outputs into a single integrated result, and nothing lands anything onto the base branch. The topological merge-order logic that would sequence this exists but is never invoked from the deployed path. The end state of a multi-agent run is N disconnected local branches with no consolidation.

**Evidence (at aeb5d67):**

- core/deps.py defines get_merge_order/topological_order/DependencyGraph but grep for these symbols outside core/deps.py returns zero callers. The worker path ends at complete_agent + enqueue dependents (worker.py:178-189); there is no step that merges agent branches together or into the base branch.

**Complete looks like:** A deployed integration phase merges agent branches in topological order (handling conflicts via the existing has_conflicts/merge_branch primitives) and produces one integrated branch/PR, wired into the worker or a post-run step.


### G13 · Dependent chaining silently sees nothing when an upstream agent leaves work uncommitted

**Severity:** major · **Dimensions:** delivery · **Status:** Open — re-verified unchanged at 572790f

The system tolerates agents that never commit by capturing uncommitted worktree diffs into the patch artifact. But dependent agents consume upstream work through the dep's git branch ref (merge_branch / `checkout dep_branch -- path`). If the upstream agent did not commit, its branch points only at base and the dependent inherits none of the upstream changes — with no error, just a wrong empty result. Whether work is committed depends on unguided model behavior.

**Evidence (at aeb5d67):**

- Neither the worker nor any executor commits agent work: only commit() callers are dependency-import commits (worktrees.py:258,296); codex.py/claude.py/openai.py contain no commit; no prompt/config instructs committing (grep commit in runtime/config/io = none). setup_worktree_with_deps merges/checks out from `spawnd/<run>/<dep>` branch refs (worktrees.py:238-296). Provenance even captures 'worktree […]

**Complete looks like:** The worker commits the agent's worktree changes to its branch before complete/enqueue, so the branch ref always reflects the full output that dependents and PR creation rely on.


### G14 · Worktrees and branches never cleaned up in deployed path (unbounded disk growth)

**Severity:** major · **Dimensions:** delivery, operations, realrepo · **Status:** Open — re-verified unchanged at 572790f

Every agent leaves a persistent worktree and branch in the worker's scratch root forever. Over continuous unattended operation this is an unbounded disk and git-ref leak on worker hosts, eventually exhausting disk or degrading git performance. There is no retention/GC policy wired in.

**Evidence (at aeb5d67):**

- cleanup_run_worktrees (worktrees.py:301) and remove_worktree/delete_branch have no callers outside gitops (grep). The worker creates a worktree per agent (worker.py:194) and never removes it; _execute_claimed has no cleanup. create_worktree explicitly relies on reuse-if-exists for resume (worktrees.py:70-72), so stale worktrees accumulate.
- gitops/worktrees.py:301 `cleanup_run_worktrees` and :108 `remove_worktree` exist but are called only from tests (grep across repo: sole non-def caller is tests/test_git.py:95). worker.py has zero cleanup/remove_worktree/rmtree references. Direct evidence: .spawnd-scratch/worktrees/ holds 3 leftover full checkouts from prior real-contributor runs (real-contributor-20260606224617, ...20260607083751, […]
- spawnd/gitops/worktrees.py:301-321 cleanup_run_worktrees / remove_worktree / delete_branch exist, but grep shows their only callers are inside cleanup_run_worktrees itself; DeployedWorker._execute_claimed (spawnd/workers/worker.py:97-189) never removes the worktree on success or failure, and no reaper/GC/TTL exists (grep reaper/prune/rmtree/disk across spawnd/ found nothing relevant).

**Complete looks like:** After a run reaches a terminal state (and its branch is pushed/integrated), worktrees and branches are reclaimed via the existing cleanup_run_worktrees on a schedule or post-run hook.


### G15 · HTTP API has zero authentication or authorization

**Severity:** major · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

Anyone who can reach the server can submit arbitrary plans (which run arbitrary shell as check/setup commands and drive model agents with bypassPermissions), cancel/resume any run, and read every run's artifacts/provenance/traces. There is no token, no per-tenant scoping, no identity on submitted plans. For unattended operation the API is the submission path; without auth it is an unauthenticated remote-code-execution surface the moment it is exposed beyond loopback.

**Evidence (at aeb5d67):**

- spawnd/server.py:55-111 defines all routes (POST /runs, /cancel, /resume, /workers/reconcile, GET status/events/artifacts/traces/provenance) with no auth dependency. grep for add_middleware/HTTPBearer/APIKeyHeader/Depends/Authorization across spawnd/ returns NONE FOUND. CLI serve binds 127.0.0.1 by default (cli.py:474) but the app itself is open.

**Complete looks like:** Every server.py route requires a verified credential (e.g. bearer token / API key) validated at the edge, with submission attributed to a principal, and unauthorized requests rejected before reaching submit_plan or the repository.


### G16 · Claude worker agents always run with permission_mode=bypassPermissions and no host isolation

**Severity:** major · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

Each Claude agent executes model-chosen Bash/Write/Edit with all permission prompts bypassed, directly on the worker host filesystem (the worktree shares the host's git repo and inherits the worker's full environment and credentials). There is no container, jail, resource limit, or syscall restriction. A single malicious or hallucinated command can read host secrets, exfiltrate via network (no egress control), or damage the host. Unattended this is the core safety hole: no human is present to catch a destructive command.

**Evidence (at aeb5d67):**

- spawnd/runtime/executors/claude.py:72 sets permission_mode='bypassPermissions' if toolset.write_allowed else 'plan'. executor.py:34 always calls worker_toolset(system_prompt=...) with the default write_allowed=True (toolset.py:41), so every worker is write_allowed. No code path sets write_allowed=False in deployed execution. grep for docker/container/firejail/nsjail/seccomp/chroot/unshare/rlimit/c […]

**Complete looks like:** Agent runtimes execute inside an isolated sandbox (container/jail/VM) with constrained filesystem, network egress policy, and resource limits, and the host's ambient credentials are not visible to agent processes.


### G17 · Provider API keys reach agents only via ambient env inheritance; no managed secret injection

**Severity:** major · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

There is no managed credential store or per-run/per-tenant key injection. Workers depend on whatever ANTHROPIC_API_KEY/OPENAI_API_KEY happen to be in the worker process environment, which every spawned agent subprocess also inherits (codex copies os.environ wholesale). Operators cannot scope, rotate, or attribute provider spend per run, and any agent command can read the keys from its own environment. Unattended fleets need explicit, per-run, least-privilege key delivery, not shared ambient inheritance.

**Evidence (at aeb5d67):**

- grep for ANTHROPIC_API_KEY/OPENAI_API_KEY/api_key across spawnd/ finds only the redaction regex (redaction.py:15) — never an injection point. codex.py:44 does env=os.environ.copy(); claude.py:17-26 builds env from SPAWND_* plus config.env and the SDK inherits the process env. config/__init__.py:95-108 (BackendConfig) loads DB/Redis/artifacts/telemetry but no provider keys. The only per-run key pat […]

**Complete looks like:** Provider credentials are sourced from a managed secret store, injected scoped to each agent invocation (not blanket-inherited by agent subprocesses), and never readable by the model's own shell tools.


### G18 · Plan spec persisted raw in Postgres, leaking agent.env secret values

**Severity:** major · **Dimensions:** realrepo, security · **Status:** Open — re-verified unchanged at 572790f

Any secret a user places in plan agent.env (the only documented per-run config channel for passing values into setup/runtime/checks) is written to Postgres in cleartext inside runs.spec, and is returned verbatim by GET /runs/{id} (server.py:43-52 returns run_row). The redaction policy is enforced on artifacts and on the agents table but not on the durable plan spec itself — the system's own stated 'never persist secrets' contract is violated at the primary state path.

**Evidence (at aeb5d67):**

- spawnd/state/repository.py:54 stores spec = plan.model_dump(mode='json') into schema.runs.spec (schema.py runs.spec JSON column) verbatim, including each agent.env dict. The agents table redacts env (repository.py:91 env_metadata=redact_env(agent.env)), but the full runs.spec is unredacted. worker.py:102 reloads PlanSpec(**run['spec']) and passes agent.env onward. CLAUDE.md Artifact Policy says 'N […]
- spawnd/state/repository.py:54-60 stores spec = plan.model_dump(mode='json') (full AgentSpec incl. env) into runs.spec; only the separate agents.env_metadata column is redacted via redact_env (repository.py:91). The worker reconstructs PlanSpec(**run['spec']) (worker.py:101-108) and uses agent.env for setup (worker.py:237) and runtime (worker.py:301).

**Complete looks like:** agent.env secret values are stripped or referenced indirectly (e.g. secret refs resolved at execution from a vault) before runs.spec is persisted, and run status responses never echo cleartext secrets.


### G19 · Check, setup, and PR commands run arbitrary shell from unauthenticated plans

**Severity:** major · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

Check/setup commands are shell-executed on the worker host with the worker's full environment, and plan submission is unauthenticated. Combined with the open API, this means any client can submit a plan whose check command is an arbitrary shell payload that runs on the worker outside even the agent runtime. There is no allowlist, no sandbox for these commands, and no notion of who is trusted to submit plans. This is a direct command-injection-to-RCE surface for unattended operation.

**Evidence (at aeb5d67):**

- spawnd/workers/worker.py:438 runs subprocess.run(command, shell=True, cwd=worktree) for the check command; worktrees.py:102 runs setup with shell=True. Both commands come from plan YAML (specs.py defaults.check / agent.check / worktree_setup.command) submitted via the unauthenticated POST /runs (server.py:58-73). codex.py:150-152 also shlex.splits SPAWND_CODEX_EXTRA_ARGS from env into the codex ar […]

**Complete looks like:** Plan submission is authenticated and authorized, and check/setup command execution is constrained (sandboxed and/or validated) so a submitter cannot achieve arbitrary host code execution beyond the intended verification.


### G20 · No git credential handling for private-repo clone/fetch/push

**Severity:** major · **Dimensions:** realrepo, security · **Status:** Open — re-verified unchanged at 572790f

Despite source_repo being a first-class submitted/stored field, the worker never fetches or clones it — it can only run against a repo already present on the worker host at --source-path, so deployed workers cannot provision a real codebase per run, and there is no token/SSH-key mechanism for authenticating against private remotes for fetch or push. For unattended workflows on real (private) codebases, there is no wired path to get the code onto the worker or push results, beyond whatever ambient git/gh credentials the host happens to have.

**Evidence (at aeb5d67):**

- source_repo is stored (repository.py:65, schema.py:32) but never cloned: grep for 'git clone'/'clone' across spawnd/ finds only the source_repo plumbing, no clone call. The worker operates only on self.source_path (worker.py:59, cli.py:204 --source-path / cwd). gitops/worktrees.py:75 does 'fetch origin' and provenance reads 'remote get-url origin' (worker.py:482) but no credential is ever configur […]
- spawnd/gitops/worktrees.py:74-75 runs `git fetch --prune origin` with no credential setup; grep for GITHUB_TOKEN/GIT_ASKPASS/GIT_SSH/credential/deploy key/ssh-agent/x-access-token across spawnd/ returned only redaction.py:11 (a redaction regex) and worker.py:482 (remote get-url for provenance). No token-to-URL rewrite, no askpass, no SSH key handling.

**Complete looks like:** A run can specify a (private) source_repo that the worker clones/fetches and pushes to using per-run scoped git credentials, rather than depending on a preexisting local checkout and ambient host credentials.


### G21 · No deployment artifacts of any kind (no Docker/compose/k8s/systemd/Procfile)

**Severity:** major · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

There is no way to actually ship spawnd to a server. Operators must hand-roll process management, image build, env injection (SPAWND_DATABASE_URL/REDIS_URL/ARTIFACTS_*), and the server+worker+reconcile topology entirely from scratch. For a deployed-first system this is the gating gap.

**Evidence (at aeb5d67):**

- absent: `find` over whole repo for Dockerfile/docker-compose/Procfile/*.service/k8s/helm/terraform returned only test-plan.yaml. scripts/ dir is empty (ls scripts/). README.md:148-167 documents `spawnd serve` and `spawnd worker --poll` as raw commands with no container image, process manager, or deploy recipe.

**Complete looks like:** A Dockerfile plus compose/k8s manifest (or systemd units) that boots Postgres+Redis+object-store wiring, runs `alembic upgrade head`, and supervises the server and a worker fleet.


### G22 · Polling worker has no supervision and crashes the whole process on any transient error

**Severity:** major · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

A single transient Postgres or Redis hiccup (or any unhandled exception in execution) propagates out of `run_poll`, the asyncio loop exits, and the `spawnd worker --poll` process dies with no in-process restart. Unattended, the worker fleet silently shrinks to zero on the first blip; with no external supervisor (see Docker/systemd gap) nothing brings it back.

**Evidence (at aeb5d67):**

- worker.py:81-87 `run_poll` does `while True: result = await self.run_once(...)` with no try/except. `run_once` (worker.py:64-79) calls `claim_next_agent` (Postgres/Redis IO) and `_execute_claimed` outside any guard. No signal/SIGTERM/KeyboardInterrupt/restart handling anywhere (grep of workers/ + cli.py for signal/SIGTERM/shutdown returned nothing). No retry/backoff/reconnect in submission.py clai […]

**Complete looks like:** The poll loop catches and logs per-iteration errors with backoff, and/or a documented supervisor (restart=always) is the contract; graceful SIGTERM finishes the in-flight claim before exit.


### G23 · No alerting/notification on run or agent failure

**Severity:** major · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

When an unattended run fails, times out, or exceeds cost, nothing tells a human. The only failure surface is rows in Postgres that someone must poll via `spawnd status`. The `on_complete: notify` and circuit-breaker `notify_only` settings are accepted in plans but never acted on, so they are misleading dead contracts.

**Evidence (at aeb5d67):**

- grep for webhook/notify/alert/slack/pagerduty/notification across spawnd/ found only dead config fields: specs.py:110 `on_complete: 'none'|'notify'` and CircuitBreaker.action 'notify_only' (specs.py:30). grep confirms `on_complete` has zero readers outside its definition; no dispatcher, HTTP callback, or queue emission on fail_agent (repository.py) exists.

**Complete looks like:** A failure/completion hook (webhook or pub/sub event) fires on terminal run/agent transitions, with `on_complete: notify` wired to it; or those config fields are removed.


### G24 · Run-level cost budget (total_usd/on_exceed) and circuit breaker stored but never enforced

**Severity:** major · **Dimensions:** operations, runtime · **Status:** Open — re-verified unchanged at 572790f

A runaway fan-out (manager spawning many agents, retries, or a dependency chain) can blow far past the plan's `total_usd` budget because nothing stops a run when the aggregate cost or repeated-failure threshold is crossed. Per-agent caps don't bound run-level spend. Unattended, this is the difference between a $25 run and an unbounded bill.

**Evidence (at aeb5d67):**

- CostBudget.on_exceed (specs.py:25 'pause'|'cancel'|'warn') has zero readers (grep on_exceed → only its definition). runs.max_cost_usd defaults 25 (schema.py:51) and total_cost_usd is summed on every completion (repository.py:457-465) but never compared to the cap. CircuitBreaker.threshold/action (specs.py:27-30) and orchestration.stuck_threshold (specs.py:69) also have zero readers. Only PER-AGENT […]
- PlanSpec accepts cost_budget (on_exceed: pause/cancel/warn) and orchestration.circuit_breaker (threshold/action) (spawnd/models/specs.py:22-30,63). cost_budget.total_usd is persisted to runs.max_cost_usd (spawnd/state/repository.py:64), but grep 'on_exceed|circuit|threshold|stuck' over spawnd/workers, spawnd/coordination, spawnd/state returns no enforcement logic — only the column write. The worke […]

**Complete looks like:** On each cost rollup the run total is checked against runs.max_cost_usd and on_exceed is honored (pause/cancel via existing cancel path); circuit_breaker threshold halts a run after N agent failures.


### G26 · Server exposes no health/readiness endpoint and no metrics endpoint

**Severity:** major · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

A load balancer or k8s probe has nothing to hit to know the API is up, the DB/Redis are reachable, or the worker fleet is alive. There is no Prometheus/metrics surface for queue depth, worker count, run throughput, or failure rate — only OTEL spans, which are traces not operational metrics. Unattended fleets can't be monitored or auto-restarted on liveness failure.

**Evidence (at aeb5d67):**

- server.py:55-111 defines only /runs* and /workers/reconcile routes — no /health, /healthz, /ready, or /metrics (grep for health/readiness/metrics/prometheus across spawnd/ returned nothing). pyproject.toml has no prometheus/statsd dependency; the only observability is OTEL trace export (telemetry.py:53-68).

**Complete looks like:** server.py serves /healthz (process up) and /readyz (Postgres+Redis reachable), and a /metrics endpoint (or OTEL metrics) exports queue depth, active workers, and run/agent failure counters.


### G27 · Migration story is greenfield-only; no incremental migrations for schema evolution at deploy time

**Severity:** major · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

The deploy-time DB story works for a fresh install (`alembic upgrade head` creates everything). But there is no path for evolving the 32-table schema on an existing deployment — the sole migration recreates the world, and schema.py changes will silently drift from the live DB with no incremental migration or drift check. First post-launch schema change has no safe deploy path.

**Evidence (at aeb5d67):**

- Only one migration exists: spawnd/migrations/versions/0001_deployed_backend.py whose upgrade() is just `metadata.create_all(op.get_bind())` (lines 19-20). Schema has 32 tables (grep '= Table(' schema.py = 32). Any future edit to schema.py is invisible to Alembic autogenerate compare unless a new revision is authored; env.py sets target_metadata but there is no autogenerate/compare CI guard.

**Complete looks like:** A CI check fails on schema/migration drift, and schema changes ship as incremental Alembic revisions rather than relying on create_all.


### G32 · Multiple worker processes share one source repo .git with no locking; cwd mutated process-globally

**Severity:** major · **Dimensions:** realrepo · **Status:** Partially addressed by PR #6

Standard unattended scaling (run several workers on a host against the same pre-cloned repo) drives concurrent worktree/branch/fetch operations on one .git plus an os.chdir that is global to the process; git's index/ref updates under concurrency can intermittently fail. There is also no guard that the worker's source_path matches the run's source_repo or sits at the intended ref, so a dirty/stale checkout silently becomes the base.

**Evidence (at aeb5d67):**

- spawnd/workers/worker.py:636-642 _pushd does os.chdir on the shared process; worker.py:121 wraps execution in _pushd(self.source_path); create_worktree/fetch (worktrees.py:74-79) and branch creation all run against the single shared repo. Nothing serializes concurrent `git worktree add`/`fetch`/branch ops when several workers point at the same source_path.

**Complete looks like:** Worktree/branch/fetch operations are serialized per source repo (or each worker uses an isolated clone), the worker verifies/normalizes the source repo+ref before forking, and cwd is not mutated process-globally.

**Post-merge detail (572790f):**

PR #6 added a source-repo guard but did NOT serialize git ops or stop process-global cwd mutation. Evaluating the gap's three "complete" criteria against HEAD=572790f:

(1) SERIALIZE worktree/branch/fetch per repo (or isolated clone) — UNCHANGED/OPEN. spawnd/gitops/worktrees.py:74-79 still runs `git fetch --prune origin` then `git worktree add -b spawnd/<run>/<agent>` against the shared repo with zero locking; branch creation (worktrees.py:69,76) and remove/delete (worktrees.py:108-118) likewise. Grep across spawnd/ finds no flock/fcntl/FileLock/pg_advisory/threading.Lock/asyncio.Lock/Semaphore guarding any git op (worker.py:801-807 are the only os.chdir hits). Worker model is one-agent-at-a-time per process (worker.py:73-96 run_once/run_poll), so the live failure mode is exactly the gap's: several worker processes on one host (CLI `--poll`, cli.py:216) pointed at the same pre-cloned source_path still concurrently mutate one .git index/refs — intermittent failures remain unmitigated.

(2) VERIFY/NORMALIZE source repo + ref before forking — HALF ADDRESSED. New _resolve_run_source (worker.py:247-266), wired into the live path at worker.py:131 before _pushd/_prepare_worktree, now fails the agent if run.source_repo is missing (worker.py:253-254) or not a git repo (worker.py:255-258) and resolves the canonical toplevel (worker.py:259). This closes the "source_path doesn't match a real repo" half. But it only records/passes base_ref into create_worktree (worker.py:260-266,273-274 -> worktrees.py:77-79); it never fetches, resets, or verifies the shared checkout is clean/at the intended ref. When base_ref is None, `git worktree add -b` branches off the shared repo's current HEAD (worktrees.py:76-79), so a dirty/stale shared checkout still silently becomes the base — the stale-base half persists.

(3) cwd NOT mutated process-globally — UNCHANGED/OPEN. _pushd (worker.py:801-807) still does os.chdir(path)/os.chdir(old) on the whole process, still wrapping execution at worker.py:157 (target changed from self.source_path to source.repo_path only). All git ops inside now pass explicit cwd= (worktrees.py run_git cwd=repo; checks worker.py:561 cwd=worktree; provenance cwd=worktree), making the chdir largely vestigial, but the process-global mutation mechanism is unchanged.

No isolated-clone or serialization config was added: WorktreeSource spec is still just base_ref/fetch (specs.py:44-47). New tests (tests/test_deployed_worker.py, tests/test_agent_run.py) exercise the source-resolution guard, not concurrency safety.

**Caveats:** Improvement is real and wired into the deployed path, but narrow: it only validates that the source is an existing git repo and normalizes to its toplevel. The two load-bearing parts of the gap remain — (a) no per-repo serialization/isolation for concurrent worktree/fetch/branch ops across multiple worker processes on a shared .git, and (b) the shared checkout's ref/cleanliness is never normalized, so an unconfigured base_ref still branches off whatever HEAD/dirty state the shared repo happens to be in. Process-global os.chdir persists but is now mostly inert since all git ops use explicit cwd. Did not run tests (re-verification only); SPAWND_TEST_DATABASE_URL not set, so Postgres state tests would skip regardless.


### G34 · Codex always reports cost 0.0, so per-agent and plan cost budgets are blind to all Codex spend

**Severity:** major · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

Running Codex unattended means max_cost_usd is never enforced and all Codex token spend is invisible to cost accounting. A cost-budget-driven 'pause/cancel' policy cannot protect a Codex-based run from runaway spend.

**Evidence (at aeb5d67):**

- CodexExecutor hard-codes cost: 'cost': 0.0 in both success and failure returns and cost_usd=0.0 in observer.usage (spawnd/runtime/executors/codex.py:257-273). The cost-exceeded guard exists only in claude.py:101 and openai.py:81; codex.py has no max_cost_usd check. spawnd/core/budget.py only prices gpt-* and Claude SDK self-reports — there is no Codex cost estimation. The worker records cost from […]

**Complete looks like:** Codex token usage is priced into USD (or USD is read from the SDK if available) so per-agent max_cost_usd and plan cost budget see real Codex spend and can enforce limits.


### G36 · Codex approval/sandbox policy is process-env only, not per-agent in the plan; default deny_all is unattended-risky and unvalidated

**Severity:** major · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

Whether a Codex agent can actually make edits unattended hinges on a process-level env default (deny_all approvals + workspace-write sandbox) that is not declared per agent, not persisted, and not validated. Operators can't express 'this agent may run commands' vs 'this one is read-only' in the plan, and a misconfigured worker env silently makes every Codex agent unable (or dangerously able) to act.

**Evidence (at aeb5d67):**

- Codex sandbox and approval mode come ONLY from worker-process env vars SPAWND_CODEX_SANDBOX (default workspace-write) and SPAWND_CODEX_APPROVAL_MODE (default deny_all) (spawnd/runtime/executors/codex.py:97-118), read from os.environ.copy() (codex.py:44). There is no plan/AgentSpec field for them, so per-agent overrides are impossible and a mixed fleet shares one process-wide policy. The agents.san […]

**Complete looks like:** Codex sandbox/approval policy is declared per agent in the plan, validated, passed explicitly to the executor, and recorded in agents.sandbox_policy so unattended Codex behavior is plan-driven and auditable rather than process-env-driven.


### G39 · Manager dynamic spawn_worker / mark_plan_complete are no-ops with no consumer

**Severity:** major · **Dimensions:** promise · **Status:** Open — re-verified unchanged at 572790f

The system advertises manager agents that orchestrate dynamically spawned workers (manager toolset, manager system prompt, run_manager dispatch, CLI roles like 'architect'). But a manager's spawn_worker call produces only a durable event that nothing acts on, so the spawned worker is never scheduled or executed. An unattended manager-driven plan will believe it spawned workers, mark itself complete, and finish having done none of the delegated work.

**Evidence (at aeb5d67):**

- spawnd/tools/manager.py:34-57 spawn_worker only calls append_event('spawn_worker_requested', ...) and returns a string; it inserts no agent row and enqueues nothing. mark_plan_complete (manager.py:121-125) only appends 'manager_completion_signal'. grep for consumers: 'spawn_worker_requested' and 'manager_completion_signal' appear ONLY at their producer sites (manager.py:47,124) with no reader; gre […]

**Complete looks like:** spawn_worker creates a queued agent row (with depends_on/parent wiring) and enqueues it through the outbox+Redis so a worker claims and runs it, and the manager's completion is gated on those spawned workers reaching terminal state.


### G41 · No scheduler/cron: runs never start on a recurring basis

**Severity:** minor · **Dimensions:** trigger · **Status:** Open — re-verified unchanged at 572790f

There is no way to say 'this workflow runs every night / every hour'. Every run requires an explicit, externally-timed submit call. Unattended-on-a-schedule operation (the core of 'runs without a human typing a command') simply does not exist in the codebase.

**Evidence (at aeb5d67):**

- absent: grep -rinE 'cron|apscheduler|schedule|recurring|periodic|interval.*submit' across spawnd/ returns 0 scheduling hits; pyproject.toml has no apscheduler/celery/cron/rq/dramatiq dep (only fastapi/uvicorn). RunConfig/PlanSpec/Orchestration in spawnd/models/specs.py:7-110 have no schedule/cron field. cli.py main group (lines 113-505) exposes run/submit/worker/reconcile/status/etc. but no schedu […]

**Complete looks like:** A durable schedule (cron expression + plan template) is persisted in Postgres and a recurring process submits a new run when each schedule is due, surviving restarts.


### G42 · No run templates or 'workflow runs on every X' concept

**Severity:** minor · **Dimensions:** trigger · **Status:** Open — re-verified unchanged at 572790f

There is no durable, reusable workflow definition that a trigger could instantiate per-event (e.g. per-PR with the PR's ref/number bound in). Each run is a one-shot fully-specified plan, so even with an external trigger there is nothing to parameterize against incoming event data.

**Evidence (at aeb5d67):**

- submit_plan (spawnd/state/submission.py:12-24) and create_run take a fully-materialized PlanSpec; there is no stored template/parameter-binding type. Parameterization exists only at authoring time and only against the local filesystem: plan_builder.expand_pattern_agents (spawnd/io/plan_builder.py:69-87) globs base.glob(pattern) with a {file} placeholder. absent: grep 'template|parameteriz|per-?pr| […]

**Complete looks like:** A persisted plan template accepts named parameters (repo, ref, pr_number, changed_files) and is rendered into a concrete PlanSpec at trigger time.


### G43 · No queue-driven submission from external systems (Redis is wakeup-only, no submit ingress)

**Severity:** minor · **Dimensions:** trigger · **Status:** Open — re-verified unchanged at 572790f

There is no ingress where an upstream system drops a 'do this workflow' message and spawnd picks it up. Submission is strictly synchronous push (HTTP/Python). For unattended operation embedded in a larger automation fabric, the only integration point is the live HTTP server, which must be running and reachable.

**Evidence (at aeb5d67):**

- RedisCoordinator is used for enqueue_agent/read_agent wakeups, heartbeats, cancel, events (cli.py:399, submission.py:65-79). The only ways to create a run are submit_plan callers: CLI run/submit (cli.py:128-167), api.submit/run (api.py:88-127), POST /runs (server.py:58). absent: no consumer that reads a message-bus/topic/external queue and calls submit_plan; grep for kafka/sqs/pubsub-submit/'consu […]

**Complete looks like:** A submission consumer subscribes to an external queue/topic and creates runs from validated messages, decoupling run initiation from a live synchronous caller.


### G44 · Pending queue_outbox rows are never drained; delivery relies on the producer not crashing

**Severity:** minor · **Dimensions:** resilience · **Status:** Open — re-verified unchanged at 572790f

If a process dies after the outbox INSERT commits but before the Redis enqueue, the outbox row is permanently 'pending' and the wakeup is lost. The intended at-least-once delivery guarantee of the outbox pattern is not realized; recovery only works because reconcile happens to re-derive ready agents from canonical state — but reconcile itself is manual (see first gap). The outbox table is effectively decorative.

**Evidence (at aeb5d67):**

- Outbox is written then published inline by the same caller: record_queue_outbox -> enqueue_agent -> mark_outbox_published in submission.py:26-33, submission.py:48-50, and worker.py:556-558. The Postgres insert and the Redis xadd are not in one transaction. No code ever queries queue_outbox WHERE status='pending' to redeliver — grep for queue_outbox shows only record/mark_published and no pending-d […]

**Complete looks like:** A relay drains queue_outbox rows where status='pending' (independent of the producer) and only then marks them published, giving real at-least-once wakeup delivery.


### G45 · cost_exceeded is collapsed into a generic 'failed' with no terminal distinction or non-retry guard

**Severity:** minor · **Dimensions:** resilience · **Status:** Open — re-verified unchanged at 572790f

Budget-exhausted agents are recorded as generic failures, losing the signal that distinguishes 'ran out of money' from 'crashed'. Because resume/expire retry logic keys off status, a cost_exceeded agent mislabeled 'failed' can be retried and re-spend, partially defeating the per-agent cost budget under unattended retry.

**Evidence (at aeb5d67):**

- claude/openai executors return status='cost_exceeded' (claude.py:101-106, openai.py:81-86). In the worker, _run_runtime treats any non-'completed' status as failure and calls fail_agent (worker.py:368-381), which writes status='failed' (repository.py:391) — not 'cost_exceeded'. The 'cost_exceeded' terminal value exists in schema/terminal sets (repository.py:196,420,438,452) and in resume eligibili […]

**Complete looks like:** A cost_exceeded runtime result transitions the agent to the cost_exceeded terminal state and is excluded from automatic retry.


### G46 · Free-form redaction misses bare secrets (non-assignment tokens)

**Severity:** minor · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

Agent runtime output, setup/check stdout, and patch bundles routinely contain secrets not in shell KEY=value form (curl Bearer headers, JSON payloads, printed env values, provider key prefixes). These slip past redaction and land in object storage, which the unauthenticated GET /runs/{id}/artifacts + CLI logs then surface. Redaction is best-effort but structurally incomplete for the common leak shapes in real codebases.

**Evidence (at aeb5d67):**

- spawnd/artifacts/redaction.py:14-17 ASSIGNMENT_SECRET only matches KEY=value where KEY contains a sensitive substring. redact_freeform_text (redaction.py:34-40) applies only that regex. A bare 'sk-ant-...' / 'sk-...' token, a JSON value like "token": "...", a Bearer header, or an echoed key not in KEY=value form passes through to the artifact store unredacted (store.py:109).

**Complete looks like:** redact_freeform_text also masks well-known secret token shapes (provider key prefixes, Bearer tokens, JSON secret-keyed values), not only KEY=value assignments.


### G47 · Readonly/reviewer worker capability defined in contract but unwired in deployed path

**Severity:** minor · **Dimensions:** security · **Status:** Open — re-verified unchanged at 572790f

There is a real readonly execution mode in the runtime contract, but no plan-level knob reaches it: every deployed worker, including ones a user labels 'reviewer', still gets full write tools and bypassPermissions. For unattended operation this removes a cheap, available mitigation — being able to declare investigate/review agents that physically cannot mutate the worktree.

**Evidence (at aeb5d67):**

- toolset.py:41-44 supports write_allowed=False (strips to READONLY_CODE_TOOLS; claude.py:72 maps to permission_mode='plan'; openai.py:59 honors it), and roles.py defines a 'reviewer' role. But executor.py:34 run_worker always builds worker_toolset() with default write_allowed=True, and nothing in AgentSpec/Defaults (specs.py) maps a role or field to write_allowed (grep write_allowed/readonly in mod […]

**Complete looks like:** A plan/agent field (or the reviewer role) maps to write_allowed=False so designated agents run read-only (Claude permission_mode='plan'), wired through resolve_agent_plan_config into the toolset built in executor.py.


### G48 · No fleet/queue-depth visibility command and no enforced concurrency limits

**Severity:** minor · **Dimensions:** operations · **Status:** Open — re-verified unchanged at 572790f

Operators can't see how many workers are alive or how deep the ready queue is without raw SQL, and there is no cap on how many agents run concurrently (per run or globally). On a real codebase a wide fan-out can saturate the host's CPU/git/disk with no backpressure. Workaround exists (manual SQL, OS-level limits), so this is friction rather than a hard blocker.

**Evidence (at aeb5d67):**

- CLI (cli.py) has no command to list worker_nodes or queue depth — worker_nodes table exists (schema.py:615) and heartbeats are recorded (worker.py:89-95) but nothing reads them back. grep for concurrency/max_workers/semaphore/max_parallel/in_flight across spawnd/ found only an unrelated comment in tools/factory.py:18. Scaling is purely 'run more `spawnd worker --poll`' (README.md:102-106) with no […]

**Complete looks like:** A `spawnd workers`/`spawnd queue` command surfaces live worker_nodes and ready-agent depth, and an optional max-in-flight cap provides backpressure.


### G49 · Per-worktree setup re-runs full dependency install for every agent; no caching

**Severity:** minor · **Dimensions:** realrepo · **Status:** Open — re-verified unchanged at 572790f

On a large monorepo a fan-out of N agents triggers N full dependency installs (pip/npm), multiplying setup wall-clock and cost. Worktrees share the source object store but not build/dep artifacts, so unattended large-repo runs are slow and may hit setup timeouts.

**Evidence (at aeb5d67):**

- spawnd/workers/worker.py:220-262 _run_setup_if_needed invokes worktree_setup.command for each agent's fresh worktree with no shared/cached layer; spawnd/gitops/worktrees.py:89-106 runs the command in the worktree cwd. grep for venv/cache/shared-setup found no cross-agent reuse.

**Complete looks like:** Setup can populate or link a shared, content-addressed dependency cache (e.g. shared venv/node_modules or package cache) reused across a run's worktrees instead of reinstalling per agent.


### G50 · No user-configurable MCP servers: plans cannot grant agents external MCP tools

**Severity:** minor · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

Real unattended workflows on real codebases routinely need external tools (GitHub/Jira/DB/search MCP servers) for the agent to file PRs, query issue trackers, or read external context. spawnd offers no way to declare these per agent in the plan, limiting agents to local code tools plus spawnd coordination.

**Evidence (at aeb5d67):**

- AgentSpec/PlanSpec (spawnd/models/specs.py:82-110) have no mcp_servers field. The Claude executor registers ONLY the internal spawnd coord server (spawnd/runtime/executors/claude.py:63,68); the OpenAI executor wires no MCP at all (grep 'mcp|MCPServer|HostedMCP' over openai.py / tools/openai_code.py / tools/factory_openai.py = none). A runtime_mcp_servers table exists (spawnd/state/schema.py:542-55 […]

**Complete looks like:** PlanSpec/AgentSpec accept an mcp_servers declaration that each executor wires into its runtime (Claude mcp_servers, Codex config, OpenAI hosted/stdio MCP), validated at the edge and recorded in runtime_mcp_servers.


### G51 · No session resume/continuation: stored vendor_session_id/thread_id is write-only, retries restart cold

**Severity:** minor · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

When an agent is resumed after a failure, it starts a brand-new provider session and re-derives everything from the prompt; prior in-session reasoning is lost (worktree git state survives, so work isn't fully lost). For long expensive agents this wastes tokens and time on every resume, though it is a workaround-able friction rather than a correctness blocker.

**Evidence (at aeb5d67):**

- Claude session_id (claude.py:96,109,117) and Codex thread_id (codex.py:268,327) are returned and stored as a vendor_session event (spawnd/workers/worker.py:356-362) and into runtime_sessions (repository.py:550,565). But no code path reads them back: grep for resume usage shows vendor_session_id is only ever written; AgentConfig has no session/resume field (spawnd/runtime/agent_config.py:6-21) and […]

**Complete looks like:** On retry/resume, the worker passes the stored vendor session/thread id to the executor and the executor resumes the provider conversation instead of starting cold.


### G52 · Codex manager agents are rejected: 'manager' type only works on Claude/OpenAI

**Severity:** minor · **Dimensions:** runtime · **Status:** Open — re-verified unchanged at 572790f

Runtime parity gap: orchestration (manager spawning sub-workers) is unavailable on Codex. A plan author mixing Codex with manager-style coordination silently loses that capability, and because the failure isn't retryable the run stalls. Workaround: use Claude/OpenAI for manager agents.

**Evidence (at aeb5d67):**

- CodexExecutor returns failure for managers: is_manager check at spawnd/runtime/executors/codex.py:212,217-220 returns {'success': False,...,'error':'codex runtime currently supports worker agents only'}. Claude (claude.py:49-62) and OpenAI (openai.py:45-58) both support manager toolsets and spawn_worker. Since failure is non-retryable (see first gap), a plan that declares a Codex manager will term […]

**Complete looks like:** Either Codex supports manager toolsets (coordination tools wired into the Codex runtime) or plan validation rejects a Codex manager at submission with a clear error instead of failing mid-run.


### G53 · live-events polls Postgres on an interval rather than consuming the promised Redis live-event stream

**Severity:** minor · **Dimensions:** promise · **Status:** Open — re-verified unchanged at 572790f

The promised low-latency Redis live-event tail is actually a Postgres polling loop with a default 2s interval, so 'live-events' lags and adds DB load proportional to viewers. It functions, so this is friction rather than a blocker, but it contradicts the documented Redis live-event design and the events the worker publishes to Redis pubsub are never consumed by any client.

**Evidence (at aeb5d67):**

- README states 'Redis carries ... live run events' and the coordinator implements publish_event/pubsub (spawnd/coordination/redis.py:32,87,131). But the CLI `live-events` command (spawnd/cli.py:301-316) ignores Redis entirely: it loops repo.get_events(run_id) every --interval seconds and dedupes by id via a polling sleep. There is no subscribe consumer in cli.py; publish_event is only ever publishe […]

**Complete looks like:** live-events subscribes to the Redis run-event channel the coordinator already publishes to and streams events as they occur, falling back to Postgres only for backfill.


## Re-audit guidance

When re-auditing after future merges, re-verify each open gap against the current
HEAD before reporting it: several pre-merge evidence citations were already stale
after one PR (the worker now reads `run['source_repo']`), and a stale claim erodes
trust in the register. Mark a gap resolved only when the fix is wired into the
deployed execution path — a helper with no caller, or tests alone, do not count.
