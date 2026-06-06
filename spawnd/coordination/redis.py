"""Redis coordination plane for deployed workers."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

READY_STREAM = 'spawnd:agents:ready'
WORKER_GROUP = 'spawnd-workers'


@dataclass(frozen=True)
class AgentJob:
    """Ready-agent queue message."""

    run_id: str
    agent: str
    message_id: str | None = None


class CoordinationPlane(Protocol):
    """Queue/lease/live-update contract used by deployed workers."""

    def enqueue_agent(self, run_id: str, agent: str) -> None: ...
    def read_agent(self, worker_id: str, *, block_ms: int = 1000) -> AgentJob | None: ...
    def ack_agent(self, job: AgentJob) -> None: ...
    def set_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> None: ...
    def renew_lease(self, run_id: str, agent: str, lease_token: str, ttl_seconds: int) -> bool: ...
    def heartbeat(self, worker_id: str, ttl_seconds: int = 30) -> None: ...
    def publish_event(self, run_id: str, event: dict[str, Any]) -> None: ...
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
        try:
            self.redis.xgroup_create(READY_STREAM, WORKER_GROUP, id='0', mkstream=True)
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

    def publish_cancel(self, run_id: str) -> None:
        self.redis.set(_cancel_key(run_id), '1', ex=86400)
        self.redis.publish(_cancel_channel(run_id), 'cancel')

    def is_cancelled(self, run_id: str) -> bool:
        return bool(self.redis.get(_cancel_key(run_id)))


class InMemoryCoordinator:
    """Deterministic coordination plane for tests."""

    def __init__(self) -> None:
        self.jobs: deque[AgentJob] = deque()
        self.acks: list[AgentJob] = []
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
