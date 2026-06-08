"""Tests for deployed coordination and artifact helpers."""
from spawnd.artifacts.redaction import redact_freeform_text
from spawnd.artifacts.store import InMemoryArtifactStore, store_redacted_text_artifact
from spawnd.state.submission import claim_next_agent, consume_next_submission, enqueue_newly_ready_agents, submit_plan
from spawnd.coordination.redis import InMemoryCoordinator
from spawnd.models.specs import AgentSpec, Orchestration, PlanSpec
from tests.deployed_helpers import make_repo


def test_submit_plan_persists_and_enqueues_ready_agents():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='deploy',
        agents=[
            AgentSpec(name='first', prompt='first'),
            AgentSpec(name='second', prompt='second', depends_on=['first']),
        ],
    )
    run_id = submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1')
    assert run_id == 'run-1'
    assert [job.agent for job in coordinator.jobs] == ['first']
    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses['second'] == 'pending'


def test_submit_plan_honors_run_concurrency_limit_for_ready_hints():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='deploy',
        orchestration=Orchestration(concurrency_limit=1),
        agents=[
            AgentSpec(name='first', prompt='first'),
            AgentSpec(name='second', prompt='second'),
        ],
    )

    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1')

    assert [job.agent for job in coordinator.jobs] == ['first']
    claimed = claim_next_agent(repository=repo, coordinator=coordinator, worker_id='worker-1')
    assert claimed is not None
    assert repo.claim_agent('run-1', 'second', worker_id='worker-2') is None


def test_in_memory_coordinator_subscribe_events_replays_published_events():
    coordinator = InMemoryCoordinator()
    coordinator.publish_event('run-1', {'type': 'started'})
    coordinator.publish_event('run-2', {'type': 'other'})

    assert list(coordinator.subscribe_events('run-1')) == [{'type': 'started'}]


def test_claim_next_agent_claims_postgres_before_redis_lease():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    submit_plan(PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')]), repository=repo, coordinator=coordinator, run_id='run-1')
    claimed = claim_next_agent(repository=repo, coordinator=coordinator, worker_id='worker-1')
    assert claimed is not None
    _, agent = claimed
    assert agent.name == 'a'
    assert coordinator.leases['run-1', 'a'] == agent.lease_token
    assert coordinator.acks
    assert repo.get_agents('run-1')[0]['status'] == 'running'


def test_in_memory_queue_depth_tracks_ready_jobs():
    coordinator = InMemoryCoordinator()
    coordinator.enqueue_agent('run-1', 'a')
    coordinator.enqueue_agent('run-1', 'b')

    assert coordinator.queue_depth() == 2

    job = coordinator.read_agent('worker-1')
    assert job is not None
    assert coordinator.queue_depth() == 1


def test_consume_next_submission_creates_run_from_queued_plan_message():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    coordinator.enqueue_submission(
        {
            'kind': 'plan',
            'run_id': 'queued-run',
            'source_repo': '/repo',
            'source_ref': 'main',
            'plan': {'name': 'queued', 'agents': [{'name': 'a', 'prompt': 'task'}]},
        }
    )

    result = consume_next_submission(
        repository=repo,
        coordinator=coordinator,
        consumer_id='submitter-1',
        block_ms=0,
    )

    assert result == {'status': 'submitted', 'run_id': 'queued-run'}
    assert repo.get_run('queued-run')['source_repo'] == '/repo'
    assert [job.agent for job in coordinator.jobs] == ['a']
    assert coordinator.submission_acks


def test_consume_next_submission_rejects_invalid_message_and_acks():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    coordinator.enqueue_submission({'kind': 'plan', 'plan': {'name': 'bad', 'agents': [{'name': 'a', 'prompt': 'task', 'depends_on': ['missing']}]}})

    result = consume_next_submission(
        repository=repo,
        coordinator=coordinator,
        consumer_id='submitter-1',
        block_ms=0,
    )

    assert result is not None
    assert result['status'] == 'rejected'
    assert 'depends on unknown agent' in result['error']
    assert repo.list_runs() == []
    assert coordinator.submission_acks


def test_enqueue_newly_ready_agents_after_completion():
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='deploy',
        agents=[
            AgentSpec(name='first', prompt='first'),
            AgentSpec(name='second', prompt='second', depends_on=['first']),
        ],
    )
    submit_plan(plan, repository=repo, coordinator=coordinator, run_id='run-1')
    repo.complete_agent('run-1', 'first')
    ready = enqueue_newly_ready_agents('run-1', repository=repo, coordinator=coordinator)
    assert ready == []
    assert repo.ready_agents('run-1') == ['second']


def test_store_redacted_artifact_does_not_keep_secret_value():
    store = InMemoryArtifactStore()
    blob = store_redacted_text_artifact(
        store,
        run_id='run-1',
        agent='a',
        kind='log',
        text='OPENAI_API_KEY=secret-value\nhello',
    )
    stored = next(iter(store.objects.values()))
    assert 'secret-value' not in stored
    assert blob.redaction_policy == 'redacted'
    assert store.get_text(blob.uri) == stored


def test_redaction_catches_bare_tokens_bearer_headers_json_and_url_credentials():
    text = (
        'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n'
        'provider key sk-abcdefghijklmnopqrstuvwxyz123456\n'
        '{"api_key":"json-secret-value"}\n'
        'https://user:pass@example.com/repo.git\n'
        'AWS AKIAABCDEFGHIJKLMNOP done\n'
    )

    redacted = redact_freeform_text(text)

    assert 'abcdefghijklmnopqrstuvwxyz123456' not in redacted
    assert 'json-secret-value' not in redacted
    assert 'user:pass' not in redacted
    assert 'AKIAABCDEFGHIJKLMNOP' not in redacted
    assert 'Bearer <redacted>' in redacted
    assert 'https://<redacted>@example.com/repo.git' in redacted
