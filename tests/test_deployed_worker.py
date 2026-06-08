"""Tests for deployed worker state transitions."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from spawnd.artifacts.store import InMemoryArtifactStore
from spawnd.config import ResolvedTelemetryConfig
from spawnd.state.submission import submit_plan
from spawnd.coordination.redis import InMemoryCoordinator
from spawnd.state.repository import DeployedRepository
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.runtime.agent_config import resolve_agent_plan_config
from spawnd.workers.worker import DeployedWorker, WorkerRunResult, _pull_request_for_branch, _resolve_agent_env, drain_queue_outbox, reconcile_ready_agents
from spawnd.models.specs import AgentSpec, CleanupPolicy, CommandPolicy, Defaults, GitDelivery, Orchestration, PlanSpec, WorktreeSetup, WorktreeSource
from spawnd.state import schema
from tests.deployed_helpers import make_repo


def make_telemetry(repo: DeployedRepository) -> TelemetryRecorder:
    return TelemetryRecorder(
        ResolvedTelemetryConfig(enabled=False, exporter='none', capture='full', failure_policy='degrade'),
        repo,
    )


def make_git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True, capture_output=True)
    (repo / 'README.md').write_text('# Test Repo')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=repo, check=True, capture_output=True)
    return repo


def test_resolve_agent_env_uses_refs_without_persisted_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('SPAWND_SECRET_OPENAI_API_KEY', 'secret-value')
    agent = AgentSpec(
        name='a',
        prompt='task',
        env={'SAFE_FLAG': '1'},
        env_refs={'OPENAI_API_KEY': 'SPAWND_SECRET_OPENAI_API_KEY'},
    )

    assert _resolve_agent_env(agent) == {'SAFE_FLAG': '1', 'OPENAI_API_KEY': 'secret-value'}

    monkeypatch.delenv('SPAWND_SECRET_OPENAI_API_KEY')
    with pytest.raises(ValueError, match='SPAWND_SECRET_OPENAI_API_KEY'):
        _resolve_agent_env(agent)


def test_pull_request_for_branch_uses_head_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        captured.update(
            {
                'args': args,
                'cwd': cwd,
                'capture_output': capture_output,
                'text': text,
                'timeout': timeout,
            }
        )
        return subprocess.CompletedProcess(args, 0, '[{"url":"https://github.com/fntune/spawnd/pull/4","number":4}]', '')

    monkeypatch.setattr('spawnd.workers.worker.subprocess.run', fake_run)

    assert _pull_request_for_branch('spawnd/run/a', tmp_path) == ('https://github.com/fntune/spawnd/pull/4', 4)
    assert captured == {
        'args': ['gh', 'pr', 'list', '--head', 'spawnd/run/a', '--json', 'url,number', '--limit', '1'],
        'cwd': tmp_path,
        'capture_output': True,
        'text': True,
        'timeout': 15,
    }


@pytest.mark.asyncio
async def test_worker_once_executes_mock_and_records_deployed_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    artifacts = InMemoryArtifactStore()
    source_repo = make_git_repo(tmp_path, 'source')
    worktree = tmp_path / 'worktree'
    captured_worktree: dict[str, object] = {}

    def fake_create_worktree(*args, **kwargs):
        captured_worktree['args'] = args
        captured_worktree.update(kwargs)
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    def fake_git_output(args, cwd, *, check=True):
        _ = cwd
        _ = check
        if args == ['rev-parse', 'HEAD']:
            return 'a' * 40
        if args == ['branch', '--show-current']:
            return 'spawnd/run-1/a'
        if args == ['remote', 'get-url', 'origin']:
            return 'https://github.com/fntune/spawnd.git'
        if args == ['rev-parse', 'origin/main']:
            return 'b' * 40
        if args == ['merge-base', 'origin/main', 'HEAD']:
            return 'b' * 40
        if args == ['diff', '--shortstat', f"{'b' * 40}..HEAD"]:
            return '2 files changed, 3 insertions(+), 1 deletion(-)'
        if args == ['diff', '--numstat', f"{'b' * 40}..HEAD"]:
            return '2\t1\tfile.txt\n1\t0\tother.txt'
        if args == ['diff', f"{'b' * 40}..HEAD"]:
            return 'diff --git a/file.txt b/file.txt\n'
        if args in (
            ['diff', '--shortstat', 'HEAD'],
            ['diff', '--numstat', 'HEAD'],
            ['diff', 'HEAD'],
        ):
            return ''
        if args == ['log', '-1', '--pretty=%B']:
            return 'Improve deployed evidence'
        return ''

    monkeypatch.setattr('spawnd.workers.worker.create_worktree', fake_create_worktree)
    monkeypatch.setattr('spawnd.workers.worker._git_output', fake_git_output)
    monkeypatch.setattr(
        'spawnd.workers.worker._pull_request_for_branch',
        lambda branch, cwd: ('https://github.com/fntune/spawnd/pull/9', 9),
    )

    plan = PlanSpec(
        name='deploy',
        defaults=Defaults(runtime='claude', check='true'),
        orchestration=Orchestration(worktree_source=WorktreeSource(base_ref='origin/main'), git=GitDelivery(commit=False)),
        agents=[AgentSpec(name='a', prompt='task')],
    )
    submit_plan(
        plan,
        repository=repo,
        coordinator=coordinator,
        run_id='run-1',
        source_repo=str(source_repo),
        source_ref='origin/ignored',
    )

    worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=artifacts,
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=tmp_path / 'worker-default',
        use_mock=True,
    )
    result = await worker.run_once(block_ms=0)

    assert result.claimed is True
    assert result.status == 'completed'
    assert captured_worktree['repo_path'] == source_repo.resolve()
    assert captured_worktree['base_ref'] == 'origin/main'
    assert captured_worktree['fetch'] is False
    assert repo.get_run('run-1')['status'] == 'completed'
    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'completed'
    assert agent['worker_id'] is None
    attempt = repo.get_attempts('run-1', 'a')[0]
    assert attempt['status'] == 'completed'

    invocations = repo.get_runtime_invocations('run-1', 'a')
    invocation_by_kind = {row['kind']: row for row in invocations}
    assert set(invocation_by_kind) == {'runtime', 'check'}
    check = repo.get_checks('run-1', 'a')[0]
    assert check['exit_code'] == 0
    assert check['attempt_id'] == attempt['id']
    assert check['runtime_invocation_id'] == invocation_by_kind['check']['id']
    assert check['cwd_locator'] == str(worktree)
    artifacts = repo.get_artifacts('run-1', 'a')
    assert {row['kind'] for row in artifacts} >= {'runtime-output', 'check-output', 'patch'}
    assert all(row['attempt_id'] == attempt['id'] for row in artifacts)
    provenance = repo.get_git_provenance('run-1', 'a')[0]
    assert provenance['base_ref'] == 'origin/main'
    assert provenance['base_sha'] == 'b' * 40
    assert provenance['merge_base_sha'] == 'b' * 40
    assert provenance['head_sha'] == 'a' * 40
    assert provenance['commit_sha'] == 'a' * 40
    assert provenance['pr_url'] == 'https://github.com/fntune/spawnd/pull/9'
    assert provenance['pr_number'] == 9
    assert provenance['changed_files_count'] == 2
    assert provenance['insertions_count'] == 3
    assert provenance['deletions_count'] == 1
    assert provenance['patch_artifact_id'] is not None
    assert provenance['diff_stats']['range'] == f"{'b' * 40}..HEAD"
    assert repo.get_token_usage('run-1', 'a')[0]['scope'] == 'result_total'
    assert repo.get_cost_usage('run-1', 'a')[0]['source'] == 'fake'
    assert repo.fetch_trace_spans('run-1', 'a')


@pytest.mark.asyncio
async def test_runtime_timeout_marks_agent_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    plan = PlanSpec(
        name='deploy',
        defaults=Defaults(runtime='claude', runtime_timeout_seconds=1),
        agents=[AgentSpec(name='a', prompt='task')],
    )
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    session_id = repo.record_runtime_session(
        attempt_id=claimed.attempt_id,
        run_id=claimed.run_id,
        agent=claimed.name,
        provider='anthropic',
        runtime='claude_sdk',
    )

    async def slow_mock(config):
        _ = config
        await asyncio.sleep(5)
        return {'success': True, 'status': 'completed'}

    monkeypatch.setattr('spawnd.workers.worker.run_worker_mock', slow_mock)
    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        use_mock=True,
    )
    hydrated = resolve_agent_plan_config(plan.agents[0], plan.defaults)

    status, result = await worker._run_runtime(plan, plan.agents[0], claimed, worktree, hydrated, session_id, 'anthropic', {})

    assert status == 'timeout'
    assert result['status'] == 'timeout'
    assert repo.get_agent('run-1', 'a')['status'] == 'timeout'
    assert repo.get_runtime_invocations('run-1', 'a')[0]['status'] == 'timeout'


def test_check_timeout_records_timeout_failure(tmp_path: Path):
    repo = make_repo()
    plan = PlanSpec(name='deploy', defaults=Defaults(check_timeout_seconds=1), agents=[AgentSpec(name='a', prompt='task')])
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    session_id = repo.record_runtime_session(
        attempt_id=claimed.attempt_id,
        run_id=claimed.run_id,
        agent=claimed.name,
        provider='anthropic',
        runtime='claude_sdk',
    )
    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        use_mock=True,
    )
    command = f'{sys.executable} -c "import time; time.sleep(5)"'

    status = worker._run_check(
        claimed,
        command,
        worktree,
        session_id,
        timeout_seconds=1,
        command_policy=CommandPolicy(mode='unrestricted'),
    )

    assert status == 'timeout'
    assert repo.get_agent('run-1', 'a')['status'] == 'timeout'
    check = repo.get_checks('run-1', 'a')[0]
    assert check['exit_code'] == 124


def test_worker_uses_submitted_source_ref_when_plan_has_no_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    source_repo = make_git_repo(tmp_path, 'source')
    worker_default = make_git_repo(tmp_path, 'worker-default')
    worktree = tmp_path / 'worktree'
    captured_worktree: dict[str, object] = {}

    def fake_create_worktree(*args, **kwargs):
        captured_worktree['args'] = args
        captured_worktree.update(kwargs)
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    monkeypatch.setattr('spawnd.workers.worker.create_worktree', fake_create_worktree)

    plan = PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')])
    repo.create_run(plan, 'run-1', source_repo=str(source_repo), source_ref='origin/main')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1')
    assert claimed is not None

    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=worker_default,
        use_mock=True,
    )
    source = worker._resolve_run_source(repo.get_run('run-1'), plan)
    worker._prepare_worktree(plan, plan.agents[0], claimed, source)

    assert source.repo_path == source_repo.resolve()
    assert source.base_ref == 'origin/main'
    assert captured_worktree['repo_path'] == source_repo.resolve()
    assert captured_worktree['base_ref'] == 'origin/main'


def test_worker_plan_source_ref_explicitly_overrides_submitted_ref(tmp_path: Path):
    repo = make_repo()
    source_repo = make_git_repo(tmp_path, 'source')
    plan = PlanSpec(
        name='deploy',
        orchestration=Orchestration(worktree_source=WorktreeSource(base_ref='feature/base', fetch=True)),
        agents=[AgentSpec(name='a', prompt='task')],
    )
    repo.create_run(plan, 'run-1', source_repo=str(source_repo), source_ref='origin/main')

    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
    )
    source = worker._resolve_run_source(repo.get_run('run-1'), plan)

    assert source.repo_path == source_repo.resolve()
    assert source.base_ref == 'feature/base'
    assert source.fetch is True


@pytest.mark.asyncio
async def test_worker_fails_claimed_agent_when_source_repo_is_missing(tmp_path: Path):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')])
    submit_plan(
        plan,
        repository=repo,
        coordinator=coordinator,
        run_id='run-1',
        source_repo=str(tmp_path / 'missing'),
    )
    worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=tmp_path,
        use_mock=True,
    )

    result = await worker.run_once(block_ms=0)

    assert result.claimed is True
    assert result.status == 'failed'
    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'failed'
    errors = repo.get_runtime_errors('run-1', 'a')
    assert errors[0]['source'] == 'worktree_source'
    assert 'does not exist' in errors[0]['message_preview']
    assert {row['kind'] for row in repo.get_artifacts('run-1', 'a')} == {'source-error'}


@pytest.mark.asyncio
async def test_worker_blocks_write_runtime_without_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    artifacts = InMemoryArtifactStore()
    source_repo = make_git_repo(tmp_path, 'source')
    worktree = tmp_path / 'worktree'
    monkeypatch.delenv('SPAWND_RUNTIME_ISOLATION', raising=False)

    def fake_create_worktree(*args, **kwargs):
        _ = (args, kwargs)
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    monkeypatch.setattr('spawnd.workers.worker.create_worktree', fake_create_worktree)
    plan = PlanSpec(name='deploy', defaults=Defaults(runtime='claude'), agents=[AgentSpec(name='a', prompt='task')])
    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1', source_repo=str(source_repo))

    worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=artifacts,
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=source_repo,
        use_mock=False,
    )

    result = await worker.run_once(block_ms=0)

    assert result.claimed is True
    assert result.status == 'failed'
    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'failed'
    assert 'SPAWND_RUNTIME_ISOLATION' in agent['error']
    assert {row['source'] for row in repo.get_runtime_errors('run-1', 'a')} == {'runtime_isolation'}


def test_reconcile_recovers_lost_redis_ready_hint():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')])
    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1')
    coordinator.jobs.clear()

    requeued = reconcile_ready_agents(repo, coordinator)

    assert requeued == [{'run_id': 'run-1', 'agent': 'a'}]
    assert [job.agent for job in coordinator.jobs] == ['a']
    with repo.engine.connect() as conn:
        outbox = conn.execute(
            select(schema.queue_outbox).where(schema.queue_outbox.c.run_id == 'run-1')
        ).mappings().all()
    assert len(outbox) == 2
    assert {row['status'] for row in outbox} == {'published'}


def test_drain_queue_outbox_publishes_pending_rows():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')])
    repo.create_run(plan, 'run-1')
    outbox_id = repo.record_queue_outbox('run-1', 'a', 'agent_ready', {'run_id': 'run-1', 'agent': 'a'})

    published = drain_queue_outbox(repo, coordinator)

    assert published == [{'run_id': 'run-1', 'agent': 'a'}]
    assert [job.agent for job in coordinator.jobs] == ['a']
    with repo.engine.connect() as conn:
        row = conn.execute(select(schema.queue_outbox).where(schema.queue_outbox.c.id == outbox_id)).mappings().one()
    assert row['status'] == 'published'
    assert row['published_at'] is not None


def test_worker_cleanup_policy_removes_worktree_and_records_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    plan = PlanSpec(
        name='deploy',
        orchestration=Orchestration(cleanup=CleanupPolicy(worktree=True)),
        agents=[AgentSpec(name='a', prompt='task')],
    )
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1')
    assert claimed is not None
    removed = []
    monkeypatch.setattr('spawnd.workers.worker.remove_worktree', lambda worktree, repo_path: removed.append((worktree, repo_path)))
    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
    )

    worker._cleanup_worktree_if_configured(plan, claimed, tmp_path / 'worktree', tmp_path)

    assert removed == [(tmp_path / 'worktree', tmp_path)]
    assert repo.get_events('run-1')[0]['event_type'] == 'worktree_cleaned'


def test_setup_cache_key_changes_with_lockfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    worker = DeployedWorker(
        repository=repo,
        coordinator=InMemoryCoordinator(),
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=tmp_path,
    )
    source = make_git_repo(tmp_path, 'source')
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    lockfile = worktree / 'pnpm-lock.yaml'
    setup = WorktreeSetup(command='pnpm install', cache=True)
    monkeypatch.setenv('SPAWND_SETUP_CACHE_ROOT', str(tmp_path / 'cache'))

    lockfile.write_text('a')
    first = worker._setup_cache_env(setup, worktree, source)
    lockfile.write_text('b')
    second = worker._setup_cache_env(setup, worktree, source)

    assert first['SPAWND_SETUP_CACHE_KEY'] != second['SPAWND_SETUP_CACHE_KEY']
    assert first['SPAWND_SETUP_CACHE_DIR'].startswith(str(tmp_path / 'cache'))
    assert first['npm_config_store_dir'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['npm_config_cache'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['YARN_CACHE_FOLDER'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['BUN_INSTALL_CACHE_DIR'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['UV_CACHE_DIR'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['PIP_CACHE_DIR'].startswith(first['SPAWND_SETUP_CACHE_DIR'])
    assert first['POETRY_CACHE_DIR'].startswith(first['SPAWND_SETUP_CACHE_DIR'])


def test_reconcile_publishes_outbox_for_expired_retryable_lease():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='deploy',
        agents=[AgentSpec(name='a', prompt='task', on_failure='retry', retry_count=2)],
    )
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    stale = datetime.now(timezone.utc) - timedelta(seconds=5)
    with repo.engine.begin() as conn:
        conn.execute(
            update(schema.agents)
            .where(schema.agents.c.run_id == 'run-1')
            .values(leased_until=stale)
        )

    requeued = reconcile_ready_agents(repo, coordinator)

    assert requeued == [{'run_id': 'run-1', 'agent': 'a'}]
    assert [job.agent for job in coordinator.jobs] == ['a']
    with repo.engine.connect() as conn:
        outbox = conn.execute(
            select(schema.queue_outbox).where(schema.queue_outbox.c.run_id == 'run-1')
        ).mappings().all()
    assert len(outbox) == 1
    assert outbox[0]['status'] == 'published'
    assert repo.get_run('run-1')['status'] == 'queued'


def test_expire_stale_lease_requeues_retryable_agent():
    repo = make_repo()
    plan = PlanSpec(
        name='deploy',
        agents=[AgentSpec(name='a', prompt='task', on_failure='retry', retry_count=2)],
    )
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    stale = datetime.now(timezone.utc) - timedelta(seconds=5)
    with repo.engine.begin() as conn:
        conn.execute(
            update(schema.agents)
            .where(schema.agents.c.run_id == 'run-1')
            .values(leased_until=stale)
        )

    expired = repo.expire_stale_leases(now=datetime.now(timezone.utc))

    assert expired == [{'run_id': 'run-1', 'agent': 'a'}]
    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'queued'
    assert agent['retry_attempt'] == 1
    assert repo.get_attempts('run-1', 'a')[0]['status'] == 'expired'
    assert repo.get_run('run-1')['status'] == 'queued'


@pytest.mark.asyncio
async def test_worker_poll_reaps_stale_leases_before_next_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='deploy',
        agents=[AgentSpec(name='a', prompt='task', on_failure='retry', retry_count=2)],
    )
    repo.create_run(plan, 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='lost-worker', lease_seconds=60)
    assert claimed is not None
    stale = datetime.now(timezone.utc) - timedelta(seconds=5)
    with repo.engine.begin() as conn:
        conn.execute(
            update(schema.agents)
            .where(schema.agents.c.run_id == 'run-1')
            .values(leased_until=stale)
        )

    worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=InMemoryArtifactStore(),
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=tmp_path,
        use_mock=True,
    )

    async def stop_after_reap(*, block_ms: int) -> WorkerRunResult:
        _ = block_ms
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, 'run_once', stop_after_reap)

    with pytest.raises(asyncio.CancelledError):
        await worker.run_poll(block_ms=0)

    assert repo.get_agent('run-1', 'a')['status'] == 'queued'
    assert [job.agent for job in coordinator.jobs] == ['a']
