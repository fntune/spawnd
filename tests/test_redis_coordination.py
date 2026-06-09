from __future__ import annotations

import fakeredis

from spawnd.coordination.redis import AgentJob, RedisCoordinator, RunSubmissionJob


def test_read_agent_recovers_after_redis_state_is_lost() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    coordinator = RedisCoordinator(redis)

    redis.flushdb()
    assert coordinator.read_agent('worker-1', block_ms=1) is None

    coordinator.enqueue_agent('run-1', 'agent-1')
    job = coordinator.read_agent('worker-1', block_ms=1)

    assert job is not None
    assert job == AgentJob(run_id='run-1', agent='agent-1', message_id=job.message_id)


def test_read_submission_recovers_after_redis_state_is_lost() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    coordinator = RedisCoordinator(redis)

    redis.flushdb()
    assert coordinator.read_submission('submitter-1', block_ms=1) is None

    coordinator.enqueue_submission({'kind': 'plan', 'run_id': 'run-1'})
    job = coordinator.read_submission('submitter-1', block_ms=1)

    assert job is not None
    assert job == RunSubmissionJob(payload={'kind': 'plan', 'run_id': 'run-1'}, message_id=job.message_id)


def test_ack_ignores_missing_redis_group_after_state_is_lost() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    coordinator = RedisCoordinator(redis)
    coordinator.enqueue_agent('run-1', 'agent-1')
    agent_job = coordinator.read_agent('worker-1', block_ms=1)
    coordinator.enqueue_submission({'kind': 'plan', 'run_id': 'run-1'})
    submission_job = coordinator.read_submission('submitter-1', block_ms=1)
    assert agent_job is not None
    assert submission_job is not None

    redis.flushdb()

    coordinator.ack_agent(agent_job)
    coordinator.ack_submission(submission_job)


def test_queue_depth_uses_consumer_group_backlog_not_stream_history() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    coordinator = RedisCoordinator(redis)
    coordinator.enqueue_agent('run-1', 'agent-1')

    assert coordinator.queue_depth() == 1
    job = coordinator.read_agent('worker-1', block_ms=1)
    assert job is not None
    assert coordinator.queue_depth() == 1

    coordinator.ack_agent(job)

    assert redis.xlen('spawnd:agents:ready') == 1
    assert coordinator.queue_depth() == 0


def test_submission_queue_depth_uses_consumer_group_backlog_not_stream_history() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    coordinator = RedisCoordinator(redis)
    coordinator.enqueue_submission({'kind': 'plan', 'run_id': 'run-1'})

    assert coordinator.submission_queue_depth() == 1
    job = coordinator.read_submission('submitter-1', block_ms=1)
    assert job is not None
    assert coordinator.submission_queue_depth() == 1

    coordinator.ack_submission(job)

    assert redis.xlen('spawnd:runs:submit') == 1
    assert coordinator.submission_queue_depth() == 0
