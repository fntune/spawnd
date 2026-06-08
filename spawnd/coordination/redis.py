"""Redis coordination plane for deployed workers."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

READY_STREAM = 'spawnd:agents:ready'
WORKER_GROUP = 'spawnd-workers'
SUBMISSION_STREAM = 'spawnd:runs:submit'
SUBMITTER_GROUP = 'spawnd-submitters'


@dataclass(frozen=True)
class AgentJob:
    """Ready-agent queue message."""

    run_id: str
    agent: str
    message_id: str | None = None


@dataclass(frozen=True)
class RunSubmissionJob:
    """Queued run-submission message."""

    payload: dict[str, Any]
    message_id: str | None = None


class CoordinationPlane(Protocol):
    """Queue/lease/live-update contract used by deployed workers."""

    def enqueue_agent(self, run_id: str, agent: str) -> None: ...
    def read_agent(self, worker_id: str, *, block_ms: int = 1000) -> AgentJob | None: ...
    def ack_agent(self, job: AgentJob) -> None: ...
    def queue_depth(self) -> int: ...
    def enqueue_submission(self, payload: dict[str, Any]) -> None: ...
    def read_submission(self, consumer_id: str, *, block_ms: int = 1000) -> RunSubmissionJob | None: ...
    def ack_submission(self, job: RunSubmissionJob) -> None: ...
    def submission_queue_depth(self) -> int: ...
    def set_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> None: ...
    def renew_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> bool: ...
    def heartbeat(self, worker_id: str, ttl_seconds: int = 30) -> None: ...
    def publish_event(self, run_id: str, event: dict[str, Any]) -> None: ...
    def subscribe_events(self, run_id: str): ...
    def publish_cancel(self, run_id: str) -> None: ...
    def is_cancelled(self, run_id: str) -> bool: ...


class RedisCoordinator:
    """Redis Streams and key-based coordination adapter."""

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client
        self._ensure_group()

    @classmethod
    def from_url(cls, redis_url: str) -> 'RedisCoordinator':
        from redis import Redis

        return cls(Redis.from_url(redis_url, decode_responses=True))

    def _ensure_group(self) -> None:
        self._ensure_stream_group(READY_STREAM, WORKER_GROUP)
        self._ensure_stream_group(SUBMISSION_STREAM, SUBMITTER_GROUP)

    def _ensure_stream_group(self, stream: str, group: str) -> None:
        try:
            self.redis.xgroup_create(stream, group, id='0', mkstream=True)
        except Exception as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def enqueue_agent(self, run_id: str, agent: str) -> None:
        self.redis.xadd(READY_STREAM, {'run_id': run_id, 'agent': agent})

    def read_agent(self, worker_id: str, *, block_ms: int = 1000) -> AgentJob | None:
        messages = self.redis.xreadgroup(WORKER_GROUP, worker_id, {READY_STREAM: '>'}, count=1, block=block_ms)
        if not messages:
            return None
        _, entries = messages[0]
        message_id, fields = entries[0]
        return AgentJob(run_id=fields['run_id'], agent=fields['agent'], message_id=message_id)

    def ack_agent(self, job: AgentJob) -> None:
        if job.message_id:
            self.redis.xack(READY_STREAM, WORKER_GROUP, job.message_id)

    def queue_depth(self) -> int:
        return int(self.redis.xlen(READY_STREAM))

    def enqueue_submission(self, payload: dict[str, Any]) -> None:
        self.redis.xadd(SUBMISSION_STREAM, {'payload': json.dumps(payload, sort_keys=True)})

    def read_submission(self, consumer_id: str, *, block_ms: int = 1000) -> RunSubmissionJob | None:
        messages = self.redis.xreadgroup(SUBMITTER_GROUP, consumer_id, {SUBMISSION_STREAM: '>'}, count=1, block=block_ms)
        if not messages:
            return None
        _, entries = messages[0]
        message_id, fields = entries[0]
        payload = fields.get('payload') or '{}'
        return RunSubmissionJob(payload=json.loads(payload), message_id=message_id)

    def ack_submission(self, job: RunSubmissionJob) -> None:
        if job.message_id:
            self.redis.xack(SUBMISSION_STREAM, SUBMITTER_GROUP, job.message_id)

    def submission_queue_depth(self) -> int:
        return int(self.redis.xlen(SUBMISSION_STREAM))

    def set_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> None:
        self.redis.set(_lease_key(run_id, agent), lease_token, ex=ttl_seconds)

    def renew_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> bool:
        key = _lease_key(run_id, agent)
        current = self.redis.get(key)
        if current != lease_token:
            return False
        self.redis.expire(key, ttl_seconds)
        return True

    def heartbeat(self, worker_id: str, ttl_seconds: int = 30) -> None:
        self.redis.hset(_heartbeat_key(worker_id), mapping={'ts': str(time.time())})
        self.redis.expire(_heartbeat_key(worker_id), ttl_seconds)

    def publish_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.redis.publish(_event_channel(run_id), json.dumps(event, sort_keys=True))

    def subscribe_events(self, run_id: str):
        pubsub = self.redis.pubsub()
        pubsub.subscribe(_event_channel(run_id))
        try:
            for message in pubsub.listen():
                if message.get('type') != 'message':
                    continue
                data = message.get('data')
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                yield json.loads(data)
        finally:
            pubsub.close()

    def publish_cancel(self, run_id: str) -> None:
        self.redis.set(_cancel_key(run_id), '1', ex=86400)
        self.redis.publish(_cancel_channel(run_id), 'cancel')

    def is_cancelled(self, run_id: str) -> bool:
        return bool(self.redis.get(_cancel_key(run_id)))


class InMemoryCoordinator:
    """Deterministic coordination plane for tests."""

    def __init__(self) -> None:
        self.jobs: deque[AgentJob] = deque()
        self.submissions: deque[RunSubmissionJob] = deque()
        self.acks: list[AgentJob] = []
        self.submission_acks: list[RunSubmissionJob] = []
        self.leases: dict[tuple[str, str], str] = {}
        self.heartbeats: dict[str, float] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cancellations: list[str] = []

    def enqueue_agent(self, run_id: str, agent: str) -> None:
        self.jobs.append(AgentJob(run_id=run_id, agent=agent, message_id=f'{len(self.jobs) + 1}-0'))

    def read_agent(self, worker_id: str, *, block_ms: int = 1000) -> AgentJob | None:
        if not self.jobs:
            return None
        return self.jobs.popleft()

    def ack_agent(self, job: AgentJob) -> None:
        self.acks.append(job)

    def queue_depth(self) -> int:
        return len(self.jobs)

    def enqueue_submission(self, payload: dict[str, Any]) -> None:
        self.submissions.append(RunSubmissionJob(payload=dict(payload), message_id=f'{len(self.submissions) + 1}-0'))

    def read_submission(self, consumer_id: str, *, block_ms: int = 1000) -> RunSubmissionJob | None:
        if not self.submissions:
            return None
        return self.submissions.popleft()

    def ack_submission(self, job: RunSubmissionJob) -> None:
        self.submission_acks.append(job)

    def submission_queue_depth(self) -> int:
        return len(self.submissions)

    def set_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> None:
        self.leases[run_id, agent] = lease_token

    def renew_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> bool:
        if self.leases.get((run_id, agent)) != lease_token:
            return False
        return True

    def heartbeat(self, worker_id: str, ttl_seconds: int = 30) -> None:
        self.heartbeats[worker_id] = time.time()

    def publish_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append((run_id, event))

    def subscribe_events(self, run_id: str):
        for event_run_id, event in list(self.events):
            if event_run_id == run_id:
                yield event

    def publish_cancel(self, run_id: str) -> None:
        self.cancellations.append(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancellations


def _lease_key(run_id: str, agent: str) -> str:
    return f'spawnd:lease:{run_id}:{agent}'


def _heartbeat_key(worker_id: str) -> str:
    return f'spawnd:worker:{worker_id}'


def _event_channel(run_id: str) -> str:
    return f'spawnd:runs:{run_id}:events'


def _cancel_channel(run_id: str) -> str:
    return f'spawnd:runs:{run_id}:cancel'


def _cancel_key(run_id: str) -> str:
    return f'spawnd:runs:{run_id}:cancelled'
