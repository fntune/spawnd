"""Tests for deployed Postgres-style repository behavior."""
from datetime import datetime, timezone

from tests.deployed_helpers import make_repo
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec


def test_create_run_records_agents_without_raw_env_or_prompt_secret():
    repo = make_repo()
    plan = PlanSpec(
        name='deployed',
        defaults=Defaults(runtime='codex', model='sonnet'),
        agents=[
            AgentSpec(
                name='a',
                prompt='Do work with API_KEY=secret-value',
                env={'PATH': '/bin', 'OPENAI_API_KEY': 'secret-value'},
            )
        ],
    )
    repo.create_run(plan, 'run-1', source_repo='repo', source_ref='origin/main')
    run = repo.get_run('run-1')
    assert run is not None
    agents = repo.get_agents('run-1')
    assert agents[0]['status'] == 'queued'
    assert agents[0]['runtime'] == 'codex'
    assert agents[0]['model'] is None
    assert 'secret-value' not in str(agents[0])
    assert agents[0]['env_metadata']['keys'] == ['PATH']


def test_claim_agent_is_single_owner():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None
    assert claimed.worker_id == 'worker-1'
    assert repo.claim_agent('run-1', 'a', worker_id='worker-2') is None
    agents = repo.get_agents('run-1')
    assert agents[0]['status'] == 'running'
    assert agents[0]['worker_id'] == 'worker-1'
    run = repo.get_run('run-1')
    assert run is not None
    assert run['status'] == 'running'


def test_cancel_agent_releases_worker_ownership_and_attempt():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.cancel_agent('run-1', 'a', 'Manager cancelled worker') is True

    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'cancelled'
    assert agent['worker_id'] is None
    assert agent['lease_token'] is None
    assert agent['leased_until'] is None
    assert agent['heartbeat_at'] is None
    attempt = repo.get_attempts('run-1', 'a')[0]
    assert attempt['status'] == 'cancelled'
    assert attempt['finished_at'] is not None
    run = repo.get_run('run-1')
    assert run is not None
    assert run['status'] == 'cancelled'


def test_cancel_run_releases_worker_ownership_and_marks_cancelled_at():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next'),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.cancel_run('run-1') == 2

    run = repo.get_run('run-1')
    assert run is not None
    assert run['status'] == 'cancelled'
    assert run['cancelled_at'] is not None
    agents = {agent['name']: agent for agent in repo.get_agents('run-1')}
    assert {agent['status'] for agent in agents.values()} == {'cancelled'}
    assert agents['a']['worker_id'] is None
    assert agents['a']['lease_token'] is None
    assert agents['a']['leased_until'] is None
    assert agents['a']['heartbeat_at'] is None
    assert repo.get_attempts('run-1', 'a')[0]['status'] == 'cancelled'


def test_cancel_run_does_not_rewrite_completed_run():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    repo.complete_agent('run-1', 'a')

    assert repo.cancel_run('run-1') == 0

    run = repo.get_run('run-1')
    assert run is not None
    assert run['status'] == 'completed'
    assert run['cancelled_at'] is None
    assert {event['event_type'] for event in repo.get_events('run-1')} == {'done', 'run_created'}


def test_complete_agent_queues_dependents_and_records_event():
    repo = make_repo()
    plan = PlanSpec(
        name='deployed',
        agents=[
            AgentSpec(name='a', prompt='task'),
            AgentSpec(name='b', prompt='next', depends_on=['a']),
        ],
    )
    repo.create_run(plan, 'run-1')
    ready = repo.complete_agent('run-1', 'a')
    assert ready == ['b']
    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'completed', 'b': 'queued'}
    events = repo.get_events('run-1')
    assert {event['event_type'] for event in events} >= {'done', 'agent_queued', 'run_created'}


def test_repository_records_trace_artifact_check_and_git_provenance():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    now = datetime.now(timezone.utc)
    repo.record_trace_span(
        run_id='run-1',
        agent='a',
        trace_id='trace',
        span_id='span',
        parent_span_id=None,
        name='spawnd.agent',
        status='ok',
        started_at=now,
        ended_at=now,
        attributes={'token': 'secret', 'safe': 'value'},
    )
    artifact_id = repo.record_artifact(
        run_id='run-1',
        agent='a',
        kind='check-output',
        uri='s3://bucket/key',
        sha256='a' * 64,
        size_bytes=12,
        redaction_policy='redacted',
        content_type='text/plain',
    )
    repo.record_check(run_id='run-1', agent='a', command='pytest', exit_code=0, duration_ms=10, output_artifact_id=artifact_id)
    repo.record_git_provenance(run_id='run-1', agent='a', diff_stats={'files': 1}, commit_sha='b' * 40)
    spans = repo.fetch_trace_spans('run-1')
    assert spans[0]['attributes']['token'] == '<redacted>'
    assert repo.telemetry_summary('run-1')['trace_span_count'] == 1
