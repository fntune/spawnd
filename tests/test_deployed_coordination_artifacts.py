"""Tests for deployed coordination and artifact helpers."""
from spawnd.artifacts.store import InMemoryArtifactStore, store_redacted_text_artifact
from spawnd.state.submission import claim_next_agent, enqueue_newly_ready_agents, submit_plan
from spawnd.coordination.redis import InMemoryCoordinator
from spawnd.models.specs import AgentSpec, PlanSpec
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
