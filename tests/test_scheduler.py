"""Tests for scheduler module."""
import asyncio
from pathlib import Path
from typing import cast

import pytest
from spawnd.storage.db import get_agent, get_agents, get_plan, open_db, update_agent_status, update_plan_status
from spawnd.models.specs import AgentSpec, CircuitBreaker, CostBudget, DependencyContext, Orchestration, PlanSpec, WorktreeSetup, WorktreeSource
from spawnd.runtime.scheduler import Scheduler

from tests.helpers import require_row

@pytest.fixture
def temp_spawnd_dir(tmp_path):
    """Create a temporary .spawnd runs directory."""
    runs_dir = tmp_path / '.spawnd' / 'runs'
    _ = runs_dir.mkdir(parents=True)
    return tmp_path

def create_test_plan(agents: list[AgentSpec], cost_budget: CostBudget | None=None, orchestration: Orchestration | None=None) -> PlanSpec:
    """Create a test plan spec."""
    return PlanSpec(name='test-plan', agents=agents, cost_budget=cost_budget, orchestration=orchestration)

def test_scheduler_init(temp_spawnd_dir, monkeypatch):
    """Test scheduler initialization."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='test', prompt='Test task')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-001', use_mock=True)
    assert scheduler.run_id == 'test-run-001'
    assert scheduler.plan == plan
    assert scheduler.use_mock is True

def test_scheduler_init_db(temp_spawnd_dir, monkeypatch):
    """Test scheduler database initialization."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='auth', prompt='Implement auth'), AgentSpec(name='cache', prompt='Implement cache', depends_on=['auth'])]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-002', use_mock=True)
    _ = scheduler._init_db()
    db = scheduler.db
    assert db is not None
    plan_row = require_row(get_plan(db, 'test-run-002'))
    assert plan_row is not None
    assert plan_row['name'] == 'test-plan'
    agent_rows = get_agents(db, 'test-run-002')
    assert len(agent_rows) == 2
    names = {a['name'] for a in agent_rows}
    assert names == {'auth', 'cache'}
    _ = db.close()

def test_build_result_success(temp_spawnd_dir, monkeypatch):
    """Test _build_result with all agents completed."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-003', use_mock=True)
    _ = scheduler._init_db()
    _ = update_agent_status(scheduler.db, 'test-run-003', 'a', 'completed')
    result = scheduler._build_result()
    assert result.success is True
    assert result.completed == ['a']
    assert result.failed == []
    plan_row = require_row(get_plan(scheduler.db, 'test-run-003'))
    assert plan_row['status'] == 'completed'
    _ = scheduler.db.close()

def test_build_result_failure(temp_spawnd_dir, monkeypatch):
    """Test _build_result with failed agent."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-004', use_mock=True)
    _ = scheduler._init_db()
    _ = update_agent_status(scheduler.db, 'test-run-004', 'a', 'failed', 'Test error')
    result = scheduler._build_result()
    assert result.success is False
    assert result.completed == []
    assert result.failed == ['a']
    plan_row = require_row(get_plan(scheduler.db, 'test-run-004'))
    assert plan_row['status'] == 'failed'
    _ = scheduler.db.close()

def test_check_failed_deps(temp_spawnd_dir, monkeypatch):
    """Test _check_failed_deps detection."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A'), AgentSpec(name='b', prompt='Task B', depends_on=['a'])]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-005', use_mock=True)
    _ = scheduler._init_db()
    _ = update_agent_status(scheduler.db, 'test-run-005', 'a', 'failed')
    agent_b = require_row(get_agent(scheduler.db, 'test-run-005', 'b'))
    failed_deps = scheduler._check_failed_deps(agent_b)
    assert failed_deps == ['a']
    _ = scheduler.db.close()

def test_external_cancellation_detection(temp_spawnd_dir, monkeypatch):
    """Test that scheduler detects external cancellation."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-006', use_mock=True)
    _ = scheduler._init_db()
    _ = update_plan_status(scheduler.db, 'test-run-006', 'cancelled')
    plan_row = require_row(get_plan(scheduler.db, 'test-run-006'))
    assert plan_row['status'] == 'cancelled'
    _ = scheduler.db.close()

def test_resume_only_resets_retryable_agents(temp_spawnd_dir, monkeypatch):
    """Resume should not rerun agents that failed without retry policy."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='retryable', prompt='Retry me', on_failure='retry', retry_count=2), AgentSpec(name='terminal', prompt='Do not rerun', on_failure='continue')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-resume', use_mock=True)
    _ = scheduler._init_db()
    _ = update_agent_status(scheduler.db, 'test-run-resume', 'retryable', 'failed', 'boom')
    _ = update_agent_status(scheduler.db, 'test-run-resume', 'terminal', 'failed', 'boom')
    _ = scheduler.db.close()
    resumed = Scheduler(plan, run_id='test-run-resume', use_mock=True, resume=True)
    _ = resumed._init_db()
    retryable = require_row(get_agent(resumed.db, 'test-run-resume', 'retryable'))
    terminal = require_row(get_agent(resumed.db, 'test-run-resume', 'terminal'))
    assert retryable['status'] == 'pending'
    assert terminal['status'] == 'failed'
    _ = resumed.db.close()

def test_resume_requeues_paused_agents(temp_spawnd_dir, monkeypatch):
    """Manual resume should restart agents paused by cost/circuit-breaker logic."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='paused_worker', prompt='Resume me')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-paused-resume', use_mock=True)
    _ = scheduler._init_db()
    _ = update_plan_status(scheduler.db, 'test-run-paused-resume', 'paused')
    _ = update_agent_status(scheduler.db, 'test-run-paused-resume', 'paused_worker', 'paused', 'Paused by budget')
    _ = scheduler.db.close()
    resumed = Scheduler(plan, run_id='test-run-paused-resume', use_mock=True, resume=True)
    _ = resumed._init_db()
    paused_worker = require_row(get_agent(resumed.db, 'test-run-paused-resume', 'paused_worker'))
    assert paused_worker['status'] == 'pending'
    assert paused_worker['error'] is None
    _ = resumed.db.close()

@pytest.mark.asyncio
async def test_spawn_agent_propagates_agentspec_env(temp_spawnd_dir, monkeypatch):
    """AgentSpec.env should survive persist-then-rehydrate into AgentConfig."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A', env={'MY_FLAG': '1', 'TOKEN': 't'})]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-env', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-env', 'a'))
    captured: dict = {}
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', lambda *a, **kw: Path('/tmp/worktree-env'))
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *a, **kw: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *a, **kw: '')

    def fake_spawn_worker(config, use_mock=False):
        captured['env'] = config.env
        return 'task'
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', fake_spawn_worker)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', lambda *a, **kw: None)
    _ = await scheduler._spawn_agent(agent_row)
    assert captured['env'] == {'MY_FLAG': '1', 'TOKEN': 't'}
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_spawn_agent_runs_worktree_setup_before_worker(temp_spawnd_dir, monkeypatch):
    """Configured setup should run before the worker runtime starts."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    worktree = temp_spawnd_dir / 'worktree'
    _ = worktree.mkdir()
    orchestration = Orchestration(worktree_setup=WorktreeSetup(command='printf ran > setup.ok'))
    plan = create_test_plan([AgentSpec(name='a', prompt='Task A')], orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-worktree-setup', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-worktree-setup', 'a'))
    captured: dict = {}
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.get_repo_root', lambda *args, **kwargs: temp_spawnd_dir)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', lambda *args, **kwargs: worktree)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *args, **kwargs: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *args, **kwargs: '')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', lambda *args, **kwargs: None)

    def fake_spawn_worker(config, use_mock=False):
        captured['setup_exists'] = (config.worktree / 'setup.ok').exists()
        return 'task'
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', fake_spawn_worker)
    _ = await scheduler._spawn_agent(agent_row)
    assert captured['setup_exists'] is True
    assert (worktree / 'setup.ok').read_text() == 'ran'
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_spawn_agent_passes_worktree_source_config(temp_spawnd_dir, monkeypatch):
    """Worktree source config should control branch creation."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    orchestration = Orchestration(worktree_source=WorktreeSource(base_ref='origin/HEAD', fetch=True))
    plan = create_test_plan([AgentSpec(name='a', prompt='Task A')], orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-worktree-source', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-worktree-source', 'a'))
    captured: dict = {}

    def fake_create_worktree(*args, **kwargs):
        captured['base_ref'] = kwargs.get('base_ref')
        captured['fetch'] = kwargs.get('fetch')
        return temp_spawnd_dir / 'worktree'
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', fake_create_worktree)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *args, **kwargs: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *args, **kwargs: '')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', lambda *args, **kwargs: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', lambda *args, **kwargs: 'task')
    _ = await scheduler._spawn_agent(agent_row)
    assert captured == {'base_ref': 'origin/HEAD', 'fetch': True}
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_spawn_agent_fails_closed_when_worktree_setup_fails(temp_spawnd_dir, monkeypatch):
    """A failing setup command should fail the agent without launching runtime."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    worktree = temp_spawnd_dir / 'worktree'
    _ = worktree.mkdir()
    orchestration = Orchestration(worktree_setup=WorktreeSetup(command='printf bad >&2; exit 7'))
    plan = create_test_plan([AgentSpec(name='a', prompt='Task A')], orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-worktree-setup-fails', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-worktree-setup-fails', 'a'))
    spawned = {'worker': False}
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.get_repo_root', lambda *args, **kwargs: temp_spawnd_dir)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', lambda *args, **kwargs: worktree)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *args, **kwargs: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *args, **kwargs: '')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', lambda *args, **kwargs: None)

    def fake_spawn_worker(config, use_mock=False):
        spawned['worker'] = True
        return 'task'
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', fake_spawn_worker)
    task = await scheduler._spawn_agent(agent_row)
    result = await task
    failed = require_row(get_agent(scheduler.db, 'test-run-worktree-setup-fails', 'a'))
    assert spawned['worker'] is False
    assert result['status'] == 'failed'
    assert failed['status'] == 'failed'
    assert 'exit 7' in failed['error']
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_scheduler_preserves_zero_valued_agent_overrides(temp_spawnd_dir, monkeypatch):
    """Explicit zero overrides should not fall through to plan defaults."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    plan = PlanSpec(name='zero-overrides', agents=[AgentSpec(name='a', prompt='Task A', on_failure='retry', retry_count=0, max_cost_usd=0.0, max_iterations=0)])
    scheduler = Scheduler(plan, run_id='test-run-zero-overrides', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-zero-overrides', 'a'))
    assert agent_row['retry_count'] == 0
    assert agent_row['max_cost_usd'] == 0.0
    assert agent_row['max_iterations'] == 0
    captured: dict = {}
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', lambda *a, **kw: Path('/tmp/worktree-zero'))
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *a, **kw: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *a, **kw: '')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', lambda *a, **kw: None)

    def fake_spawn_worker(config, use_mock=False):
        captured['max_cost_usd'] = config.max_cost_usd
        captured['max_iterations'] = config.max_iterations
        return 'task'
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', fake_spawn_worker)
    _ = await scheduler._spawn_agent(agent_row)
    assert captured['max_cost_usd'] == 0.0
    assert captured['max_iterations'] == 0
    _ = update_agent_status(scheduler.db, 'test-run-zero-overrides', 'a', 'failed', 'boom')
    should_stop = await scheduler._handle_agent_failure('a', 'boom')
    agent_row = require_row(get_agent(scheduler.db, 'test-run-zero-overrides', 'a'))
    assert should_stop is False
    assert agent_row['status'] == 'failed'
    assert agent_row['retry_attempt'] == 1
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_spawn_agent_passes_dependency_path_filters(temp_spawnd_dir, monkeypatch):
    """Dependency path filters should be passed into worktree setup."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='base', prompt='Base task'), AgentSpec(name='child', prompt='Child task', depends_on=['base'])]
    orchestration = Orchestration(dependency_context=DependencyContext(mode='paths', include_paths=['src'], exclude_paths=['tests']))
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-path-filters', use_mock=True)
    _ = scheduler._init_db()
    agent_row = require_row(get_agent(scheduler.db, 'test-run-path-filters', 'child'))
    called: dict = {}
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.create_worktree', lambda *args, **kwargs: Path('/tmp/child-worktree'))
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.update_agent_worktree', lambda *args, **kwargs: None)
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.load_shared_context', lambda *args, **kwargs: '')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.spawn_worker', lambda *args, **kwargs: 'task')

    def fake_setup(*args, **kwargs):
        called['include_paths'] = kwargs.get('include_paths')
        called['exclude_paths'] = kwargs.get('exclude_paths')
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.setup_worktree_with_deps', fake_setup)
    _ = await scheduler._spawn_agent(agent_row)
    assert called['include_paths'] == ['src']
    assert called['exclude_paths'] == ['tests']
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_handle_cost_exceeded_warn(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with warn action."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cost_budget = CostBudget(total_usd=1.0, on_exceed='warn')
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, cost_budget=cost_budget)
    scheduler = Scheduler(plan, run_id='test-run-007', use_mock=True)
    _ = scheduler._init_db()
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is False
    plan_row = require_row(get_plan(scheduler.db, 'test-run-007'))
    assert plan_row['status'] == 'running'
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_retry_count_allows_configured_number_of_retries(temp_spawnd_dir, monkeypatch):
    """retry_count=1 should allow one retry before exhausting."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A', on_failure='retry', retry_count=1)]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-retry-count', use_mock=True)
    _ = scheduler._init_db()
    _ = update_agent_status(scheduler.db, 'test-run-retry-count', 'a', 'failed', 'boom')
    should_stop = await scheduler._handle_agent_failure('a', 'boom')
    agent_row = require_row(get_agent(scheduler.db, 'test-run-retry-count', 'a'))
    assert should_stop is False
    assert agent_row['status'] == 'pending'
    assert agent_row['retry_attempt'] == 1
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_handle_cost_exceeded_cancel(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with cancel action."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cost_budget = CostBudget(total_usd=1.0, on_exceed='cancel')
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, cost_budget=cost_budget)
    scheduler = Scheduler(plan, run_id='test-run-008', use_mock=True)
    _ = scheduler._init_db()
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is True
    plan_row = require_row(get_plan(scheduler.db, 'test-run-008'))
    assert plan_row['status'] == 'failed'
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_handle_cost_exceeded_pause(temp_spawnd_dir, monkeypatch):
    """Test cost exceeded with pause action (default)."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cost_budget = CostBudget(total_usd=1.0, on_exceed='pause')
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, cost_budget=cost_budget)
    scheduler = Scheduler(plan, run_id='test-run-009', use_mock=True)
    _ = scheduler._init_db()
    should_stop = await scheduler._handle_cost_exceeded()
    assert should_stop is True
    plan_row = require_row(get_plan(scheduler.db, 'test-run-009'))
    assert plan_row['status'] == 'paused'
    _ = scheduler.db.close()

def test_check_circuit_breaker_cancel_all(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with cancel_all action."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cb = CircuitBreaker(threshold=2, action='cancel_all')
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-010', use_mock=True)
    _ = scheduler._init_db()
    scheduler.failure_count = 2
    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is True
    plan_row = require_row(get_plan(scheduler.db, 'test-run-010'))
    assert plan_row['status'] == 'failed'
    _ = scheduler.db.close()

def test_check_circuit_breaker_pause(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with pause action."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cb = CircuitBreaker(threshold=2, action='pause')
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-011', use_mock=True)
    _ = scheduler._init_db()
    scheduler.failure_count = 2
    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is True
    plan_row = require_row(get_plan(scheduler.db, 'test-run-011'))
    assert plan_row['status'] == 'paused'
    _ = scheduler.db.close()

def test_check_circuit_breaker_notify_only(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker with notify_only action."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cb = CircuitBreaker(threshold=2, action='notify_only')
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-012', use_mock=True)
    _ = scheduler._init_db()
    scheduler.failure_count = 2
    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is False
    _ = scheduler.db.close()

def test_check_circuit_breaker_below_threshold(temp_spawnd_dir, monkeypatch):
    """Test circuit breaker below threshold."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    cb = CircuitBreaker(threshold=5, action='cancel_all')
    orchestration = Orchestration(circuit_breaker=cb)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents, orchestration=orchestration)
    scheduler = Scheduler(plan, run_id='test-run-013', use_mock=True)
    _ = scheduler._init_db()
    scheduler.failure_count = 2
    should_stop = scheduler._check_circuit_breaker()
    assert should_stop is False
    _ = scheduler.db.close()

def test_check_stuck_uses_latest_event_identity_not_capped_count(temp_spawnd_dir, monkeypatch):
    """Fresh events with a stable capped count should not look stuck."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A')]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-stuck-marker', use_mock=True)
    _ = scheduler._init_db()
    scheduler.tasks = cast(dict[str, asyncio.Task], {"a": cast(asyncio.Task, object())})
    first = [{'id': f'a{i}', 'agent': 'a', 'event_type': 'progress', 'data': '{}', 'ts': 't'} for i in range(50)]
    second = [{'id': f'b{i}', 'agent': 'a', 'event_type': 'progress', 'data': '{}', 'ts': 't'} for i in range(50)]
    events = iter([first, second])
    _ = monkeypatch.setattr('spawnd.runtime.scheduler.get_recent_events', lambda *args, **kwargs: next(events))
    assert scheduler._check_stuck() is False
    assert scheduler.idle_iterations == 0
    assert scheduler._check_stuck() is False
    assert scheduler.idle_iterations == 0
    _ = scheduler.db.close()

@pytest.mark.asyncio
async def test_cost_exceeded_is_terminal_and_not_retried(temp_spawnd_dir, monkeypatch):
    """Per-agent max cost should be terminal, not retried via on_failure=retry."""
    _ = monkeypatch.chdir(temp_spawnd_dir)
    agents = [AgentSpec(name='a', prompt='Task A', on_failure='retry', retry_count=2)]
    plan = create_test_plan(agents)
    scheduler = Scheduler(plan, run_id='test-run-cost-terminal', use_mock=True)
    spawn_count = 0

    async def fake_spawn_agent(self, agent_row):

        async def finish():
            _ = update_agent_status(self.db, self.run_id, agent_row['name'], 'cost_exceeded', 'Cost exceeded: $9.0000')
            return {'success': False, 'status': 'cost_exceeded', 'error': 'cost_exceeded'}
        nonlocal spawn_count
        spawn_count += 1
        return asyncio.create_task(finish())
    _ = monkeypatch.setattr(Scheduler, '_spawn_agent', fake_spawn_agent)
    result = await scheduler.run()
    db = open_db('test-run-cost-terminal')
    agent_row = db.execute('SELECT status, retry_attempt FROM agents WHERE run_id = ? AND name = ?', ('test-run-cost-terminal', 'a')).fetchone()
    _ = db.close()
    assert spawn_count == 1
    assert result.success is False
    assert result.failed == ['a']
    assert agent_row['status'] == 'cost_exceeded'
    assert agent_row['retry_attempt'] == 0
