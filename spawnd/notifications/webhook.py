"""Webhook notification delivery for deployed run events."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from spawnd.artifacts.redaction import redact_attributes, stable_hash
from spawnd.config import BackendConfig
from spawnd.state.repository import DeployedRepository


TERMINAL_NOTIFICATION_STATUSES = {'completed', 'failed', 'timeout', 'cancelled', 'cost_exceeded'}
FAILURE_NOTIFICATION_STATUSES = {'failed', 'timeout', 'cancelled', 'cost_exceeded'}


class NotificationDispatcher:
    """Deliver configured external notifications and persist delivery facts."""

    def __init__(self, *, repository: DeployedRepository, config: BackendConfig) -> None:
        self.repository = repository
        self.config = config

    def maybe_notify_run(self, *, plan_spec: dict[str, Any], run: dict[str, Any], agent: str | None, reason: str) -> None:
        url = self.config.notification_webhook_url
        if not url:
            return
        status = str(run.get('status') or '')
        if status not in TERMINAL_NOTIFICATION_STATUSES and reason != 'agent_failure':
            return
        on_complete = str(plan_spec.get('on_complete') or 'none') if isinstance(plan_spec, dict) else 'none'
        should_notify = status in FAILURE_NOTIFICATION_STATUSES or on_complete == 'notify' or reason == 'agent_failure'
        if not should_notify:
            return
        payload = {
            'type': 'spawnd.run.notification',
            'run_id': run.get('run_id'),
            'run_status': status,
            'agent': agent,
            'reason': reason,
            'total_cost_usd': run.get('total_cost_usd'),
            'source_repo': run.get('source_repo'),
            'source_ref': run.get('source_ref'),
        }
        try:
            _post_json(url, payload, timeout_seconds=self.config.notification_timeout_seconds)
        except Exception as exc:
            self.repository.append_event(
                str(run['run_id']),
                agent or '_system',
                'notification_error',
                {'reason': reason, 'error': str(exc)[:1000]},
            )
            return
        self.repository.append_event(
            str(run['run_id']),
            agent or '_system',
            'notification_sent',
            {'reason': reason, 'status': status, 'url_hash': stable_hash(url)},
        )


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> None:
    data = json.dumps(redact_attributes(payload), default=str).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, 'status', 200))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'notification webhook returned HTTP {exc.code}') from exc
    if status >= 400:
        raise RuntimeError(f'notification webhook returned HTTP {status}')
