"""Tests for deployed notification decisions."""
from __future__ import annotations

from spawnd.config import ArtifactStorageConfig, BackendConfig, ResolvedTelemetryConfig
from spawnd.notifications.webhook import NotificationDispatcher


class FakeRepo:
    def __init__(self) -> None:
        self.events = []

    def append_event(self, run_id, agent, event_type, data):
        self.events.append({'run_id': run_id, 'agent': agent, 'event_type': event_type, 'data': data})


def _config(url: str | None = 'https://hooks.example.test/notify?token=secret') -> BackendConfig:
    return BackendConfig(
        database_url=None,
        redis_url=None,
        api_token=None,
        github_webhook_secret=None,
        notification_webhook_url=url,
        notification_timeout_seconds=1,
        artifacts=ArtifactStorageConfig(bucket=None, endpoint=None, region=None, prefix=''),
        telemetry=ResolvedTelemetryConfig(enabled=False, exporter='none', capture='full', failure_policy='degrade'),
    )


def test_failure_notification_sends_and_persists_only_url_hash(monkeypatch):
    sent = []
    monkeypatch.setattr('spawnd.notifications.webhook._post_json', lambda url, payload, *, timeout_seconds: sent.append((url, payload, timeout_seconds)))
    repo = FakeRepo()
    dispatcher = NotificationDispatcher(repository=repo, config=_config())

    dispatcher.maybe_notify_run(
        plan_spec={'on_complete': 'none'},
        run={'run_id': 'run-1', 'status': 'failed', 'total_cost_usd': 1.25},
        agent='a',
        reason='agent_failed',
    )

    assert sent and sent[0][1]['run_id'] == 'run-1'
    assert repo.events[0]['event_type'] == 'notification_sent'
    assert 'url_hash' in repo.events[0]['data']
    assert 'secret' not in str(repo.events)


def test_completion_notification_requires_on_complete_notify(monkeypatch):
    sent = []
    monkeypatch.setattr('spawnd.notifications.webhook._post_json', lambda url, payload, *, timeout_seconds: sent.append(payload))
    repo = FakeRepo()
    dispatcher = NotificationDispatcher(repository=repo, config=_config())

    dispatcher.maybe_notify_run(
        plan_spec={'on_complete': 'none'},
        run={'run_id': 'run-1', 'status': 'completed'},
        agent='a',
        reason='agent_completed',
    )
    assert sent == []

    dispatcher.maybe_notify_run(
        plan_spec={'on_complete': 'notify'},
        run={'run_id': 'run-1', 'status': 'completed'},
        agent='a',
        reason='agent_completed',
    )
    assert sent == [{'type': 'spawnd.run.notification', 'run_id': 'run-1', 'run_status': 'completed', 'agent': 'a', 'reason': 'agent_completed', 'total_cost_usd': None, 'source_repo': None, 'source_ref': None}]
