"""Tests for deployed Postgres-style repository behavior."""
from datetime import datetime, timezone

from tests.deployed_helpers import make_repo
from spawnd.models.specs import AgentSpec, CircuitBreaker, CostBudget, Defaults, ManagerSettings, Orchestration, PlanSpec


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
    assert 'secret-value' not in str(run['spec'])
    assert run['spec']['agents'][0]['env'] == {}
    assert run['spec']['agents'][0]['env_metadata']['sensitive_key_hashes']
    agents = repo.get_agents('run-1')
    assert agents[0]['status'] == 'queued'
    assert agents[0]['runtime'] == 'codex'
    assert agents[0]['model'] is None
    assert 'secret-value' not in str(agents[0])
    assert agents[0]['env_metadata']['keys'] == ['PATH']


def test_create_run_records_reviewer_as_readonly_capability():
    repo = make_repo()
    plan = PlanSpec(
        name='review',
        agents=[AgentSpec(name='reviewer', prompt='review changes', use_role='reviewer')],
    )

    repo.create_run(plan, 'run-1')

    agent = repo.get_agent('run-1', 'reviewer')
    assert agent is not None
    assert agent['write_allowed'] is False


def test_spawn_worker_agent_creates_queued_agent_and_updates_run_spec():
    repo = make_repo()
    plan = PlanSpec(
        name='managed',
        agents=[
            AgentSpec(
                name='manager',
                type='manager',
                prompt='manage',
                manager=ManagerSettings(max_subagents=1),
            )
        ],
    )
    repo.create_run(plan, 'run-1')

    result = repo.spawn_worker_agent(
        'run-1',
        'manager',
        'manager.worker',
        prompt='do worker task',
        check='true',
        model='sonnet',
    )

    assert result == {'created': True, 'agent': 'manager.worker'}
    agent = repo.get_agent('run-1', 'manager.worker')
    assert agent is not None
    assert agent['status'] == 'queued'
    run = repo.get_run('run-1')
    dynamic = [item for item in run['spec']['agents'] if item['name'] == 'manager.worker']
    assert dynamic and dynamic[0]['prompt'] == 'do worker task'

    rejected = repo.spawn_worker_agent(
        'run-1',
        'manager',
        'manager.other',
        prompt='do another task',
    )
    assert rejected == {'created': False, 'reason': 'manager_cap_exceeded'}


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


def test_late_complete_after_cancel_run_does_not_overwrite_terminal_state():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.cancel_run('run-1') == 2

    assert repo.complete_agent('run-1', 'a', attempt_id=claimed.attempt_id) == []
    assert repo.fail_agent('run-1', 'a', 'late failure', attempt_id=claimed.attempt_id) == []
    assert repo.get_run('run-1')['status'] == 'cancelled'
    assert {agent['name']: agent['status'] for agent in repo.get_agents('run-1')} == {
        'a': 'cancelled',
        'b': 'cancelled',
    }


def test_late_complete_after_cancel_agent_does_not_queue_dependents():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.cancel_agent('run-1', 'a', 'Manager cancelled worker') is True

    assert repo.complete_agent('run-1', 'a', attempt_id=claimed.attempt_id) == []
    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'cancelled', 'b': 'pending'}


def test_consumed_clarification_response_does_not_make_item_pending_again():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    clarification_id = repo.append_event('run-1', 'manager.worker', 'clarification', {'question': 'q'})

    assert [row['id'] for row in repo.get_pending_clarifications('run-1', agent_prefix='manager.')] == [clarification_id]

    repo.record_response('run-1', clarification_id, 'answer')
    assert repo.get_pending_clarifications('run-1', agent_prefix='manager.') == []
    response = repo.get_response('run-1', clarification_id)
    assert response is not None
    repo.consume_response(response['id'])

    assert repo.get_pending_clarifications('run-1', agent_prefix='manager.') == []


def test_retryable_failure_requeues_retry_agent_and_records_attempt_failure():
    repo = make_repo()
    repo.create_run(
        PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task', on_failure='retry', retry_count=2)]),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.fail_agent('run-1', 'a', 'rate limit 429', attempt_id=claimed.attempt_id, retryable=True) == ['a']

    agent = repo.get_agent('run-1', 'a')
    assert agent is not None
    assert agent['status'] == 'queued'
    assert agent['retry_attempt'] == 1
    assert agent['error'] is None
    assert agent['last_error'] == 'rate limit 429'
    assert repo.get_attempts('run-1', 'a')[0]['status'] == 'failed'
    assert repo.get_run('run-1')['status'] == 'queued'


def test_final_failure_marks_dependency_blocked_agents_terminal():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            agents=[
                AgentSpec(name='a', prompt='task', on_failure='continue'),
                AgentSpec(name='b', prompt='next', depends_on=['a']),
                AgentSpec(name='c', prompt='independent'),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.fail_agent('run-1', 'a', 'fatal', attempt_id=claimed.attempt_id, retryable=False) == []

    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'failed', 'b': 'failed', 'c': 'queued'}
    blocked = repo.get_agent('run-1', 'b')
    assert blocked is not None
    assert blocked['error'] == 'Dependency failed: a'


def test_stop_failure_cancels_remaining_non_terminal_agents():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            agents=[
                AgentSpec(name='a', prompt='task', on_failure='stop'),
                AgentSpec(name='b', prompt='next'),
                AgentSpec(name='c', prompt='dependent', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.fail_agent('run-1', 'a', 'fatal', attempt_id=claimed.attempt_id, retryable=False) == []

    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'failed', 'b': 'cancelled', 'c': 'cancelled'}
    assert repo.get_run('run-1')['status'] == 'failed'


def test_completion_pauses_ready_work_when_run_cost_budget_is_exceeded():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            cost_budget=CostBudget(total_usd=1.0, on_exceed='pause'),
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.complete_agent('run-1', 'a', cost_usd=2.0, attempt_id=claimed.attempt_id) == []

    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'completed', 'b': 'paused'}
    assert repo.get_run('run-1')['status'] == 'paused'
    assert any(event['event_type'] == 'cost_budget_exceeded' for event in repo.get_events('run-1'))


def test_completion_warns_but_continues_when_budget_policy_is_warn():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            cost_budget=CostBudget(total_usd=1.0, on_exceed='warn'),
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.complete_agent('run-1', 'a', cost_usd=2.0, attempt_id=claimed.attempt_id) == ['b']

    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'completed', 'b': 'queued'}
    assert repo.get_run('run-1')['status'] == 'queued'
    assert any(event['event_type'] == 'cost_budget_exceeded' for event in repo.get_events('run-1'))


def test_cost_exceeded_terminal_status_rolls_up_to_run_status():
    repo = make_repo()
    repo.create_run(
        PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task', on_failure='retry', retry_count=2)]),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.fail_agent('run-1', 'a', 'Cost exceeded', attempt_id=claimed.attempt_id, retryable=True, terminal_status='cost_exceeded') == []

    assert repo.get_agent('run-1', 'a')['status'] == 'cost_exceeded'
    assert repo.get_agent('run-1', 'a')['retry_attempt'] == 0
    assert repo.get_run('run-1')['status'] == 'cost_exceeded'


def test_circuit_breaker_cancel_all_cancels_remaining_work():
    repo = make_repo()
    repo.create_run(
        PlanSpec(
            name='deployed',
            orchestration=Orchestration(circuit_breaker=CircuitBreaker(threshold=1, action='cancel_all')),
            agents=[
                AgentSpec(name='a', prompt='task'),
                AgentSpec(name='b', prompt='next'),
                AgentSpec(name='c', prompt='dependent', depends_on=['a']),
            ],
        ),
        'run-1',
    )
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1', lease_seconds=60)
    assert claimed is not None

    assert repo.fail_agent('run-1', 'a', 'fatal', attempt_id=claimed.attempt_id, retryable=False) == []

    statuses = {agent['name']: agent['status'] for agent in repo.get_agents('run-1')}
    assert statuses == {'a': 'failed', 'b': 'cancelled', 'c': 'cancelled'}
    assert repo.get_run('run-1')['status'] == 'failed'
    assert any(event['event_type'] == 'circuit_breaker_tripped' for event in repo.get_events('run-1'))


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


def test_repository_records_runtime_mcp_server_without_raw_config():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1')
    assert claimed is not None
    session_id = repo.record_runtime_session(
        attempt_id=claimed.attempt_id,
        run_id='run-1',
        agent='a',
        provider='anthropic',
        runtime='claude_sdk',
    )

    server_id = repo.record_runtime_mcp_server(
        session_id=session_id,
        name='docs',
        status='configured',
        scope='a',
        config={'type': 'http', 'url': 'https://mcp.example.test', 'header_refs': ['Authorization']},
    )

    with repo.engine.connect() as conn:
        row = conn.execute(select(schema.runtime_mcp_servers).where(schema.runtime_mcp_servers.c.id == server_id)).mappings().one()
    assert row['name'] == 'docs'
    assert row['status'] == 'configured'
    assert row['config_hash']


def test_repository_updates_and_reads_latest_provider_resume_ids():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deployed', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    claimed = repo.claim_agent('run-1', 'a', worker_id='worker-1')
    assert claimed is not None
    old_session = repo.record_runtime_session(
        attempt_id=claimed.attempt_id,
        run_id='run-1',
        agent='a',
        provider='anthropic',
        runtime='claude_sdk',
    )
    new_session = repo.record_runtime_session(
        attempt_id=claimed.attempt_id,
        run_id='run-1',
        agent='a',
        provider='anthropic',
        runtime='claude_sdk',
    )
    repo.update_runtime_session_provider_ids(old_session, provider_session_id='old')
    repo.update_runtime_session_provider_ids(new_session, provider_session_id='new')

    assert repo.latest_provider_resume_ids('run-1', 'a', 'anthropic') == {
        'provider_session_id': 'new',
        'provider_thread_id': None,
    }


def test_worker_nodes_report_stale_status():
    repo = make_repo()
    repo.record_worker_heartbeat('worker-1', hostname='host', capacity={'concurrency_limit': 1})

    workers = repo.list_worker_nodes(stale_after_seconds=60)

    assert workers[0]['worker_id'] == 'worker-1'
    assert workers[0]['hostname'] == 'host'
    assert workers[0]['capacity']['concurrency_limit'] == 1
    assert workers[0]['stale'] is False
