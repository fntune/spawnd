"""Tests for deployed worker state transitions."""
from __future__ import annotations

import subprocess
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
from spawnd.workers.worker import DeployedWorker, _pull_request_for_branch, reconcile_ready_agents
from spawnd.models.specs import AgentSpec, Defaults, Orchestration, PlanSpec, WorktreeSource
from spawnd.state import schema
from tests.deployed_helpers import make_repo


def make_telemetry(repo: DeployedRepository) -> TelemetryRecorder:
    return TelemetryRecorder(
        ResolvedTelemetryConfig(enabled=False, exporter='none', capture='full', failure_policy='degrade'),
        repo,
    )


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
        orchestration=Orchestration(worktree_source=WorktreeSource(base_ref='origin/main')),
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
