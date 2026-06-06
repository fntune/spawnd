"""Tests for deployed worker state transitions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import update

from spawnd.artifacts.store import InMemoryArtifactStore
from spawnd.config import ResolvedTelemetryConfig
from spawnd.state.submission import submit_plan
from spawnd.coordination.redis import InMemoryCoordinator
from spawnd.state.repository import DeployedRepository
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.workers.worker import DeployedWorker, reconcile_ready_agents
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
from spawnd.state import schema
from tests.deployed_helpers import make_repo


def make_telemetry(repo: DeployedRepository) -> TelemetryRecorder:
    return TelemetryRecorder(
        ResolvedTelemetryConfig(enabled=False, exporter='none', capture='full', failure_policy='degrade'),
        repo,
    )


@pytest.mark.asyncio
async def test_worker_once_executes_mock_and_records_deployed_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    artifacts = InMemoryArtifactStore()
    worktree = tmp_path / 'worktree'

    def fake_create_worktree(*args, **kwargs):
        _ = args
        _ = kwargs
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree

    def fake_git_output(args, cwd, *, check=True):
        _ = cwd
        _ = check
        if args == ['rev-parse', 'HEAD']:
            return 'a' * 40
        if args == ['branch', '--show-current']:
            return 'spawnd/run-1/a'
        if args == ['diff', '--shortstat', 'HEAD']:
            return '1 file changed, 1 insertion(+)'
        if args == ['diff', '--numstat', 'HEAD']:
            return '1\t0\tfile.txt'
        if args == ['diff', 'HEAD']:
            return 'diff --git a/file.txt b/file.txt\n'
        return ''

    monkeypatch.setattr('spawnd.workers.worker.create_worktree', fake_create_worktree)
    monkeypatch.setattr('spawnd.workers.worker._git_output', fake_git_output)

    plan = PlanSpec(
        name='deploy',
        defaults=Defaults(runtime='claude', check='true'),
        agents=[AgentSpec(name='a', prompt='task')],
    )
    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1', source_repo=str(tmp_path))

    worker = DeployedWorker(
        repository=repo,
        coordinator=coordinator,
        artifacts=artifacts,
        telemetry=make_telemetry(repo),
        worker_id='worker-1',
        source_path=tmp_path,
        use_mock=True,
    )
    result = await worker.run_once(block_ms=0)

    assert result.claimed is True
    assert result.status == 'completed'
    assert repo.get_run('run-1')['status'] == 'completed'
    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'completed'
    assert agent['worker_id'] is None
    assert repo.get_attempts('run-1', 'a')[0]['status'] == 'completed'

    invocations = repo.get_runtime_invocations('run-1', 'a')
    assert {row['kind'] for row in invocations} == {'runtime', 'check'}
    assert repo.get_checks('run-1', 'a')[0]['exit_code'] == 0
    assert {row['kind'] for row in repo.get_artifacts('run-1', 'a')} >= {'runtime-output', 'check-output', 'patch'}
    assert repo.get_git_provenance('run-1', 'a')[0]['head_sha'] == 'a' * 40
    assert repo.get_token_usage('run-1', 'a')[0]['scope'] == 'result_total'
    assert repo.get_cost_usage('run-1', 'a')[0]['source'] == 'fake'
    assert repo.fetch_trace_spans('run-1', 'a')


def test_reconcile_recovers_lost_redis_ready_hint():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')])
    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1')
    coordinator.jobs.clear()

    requeued = reconcile_ready_agents(repo, coordinator)

    assert requeued == [{'run_id': 'run-1', 'agent': 'a'}]
    assert [job.agent for job in coordinator.jobs] == ['a']


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
