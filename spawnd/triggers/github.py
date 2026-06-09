"""GitHub webhook installation for deployed trigger templates."""
from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class WebhookTarget:
    """A GitHub repository webhook target."""

    repo: str
    hook_id: int | None
    action: str
    url: str


def webhook_url(base_url: str, template_id: str) -> str:
    """Build the deployed GitHub webhook URL for a template."""

    validate_public_base_url(base_url)
    return f"{base_url.rstrip('/')}/webhooks/github/{template_id}"


def validate_public_base_url(base_url: str) -> None:
    """Reject callback URLs that cannot serve durable GitHub webhooks."""

    parsed = urlparse(base_url)
    if parsed.scheme != 'https':
        raise ValueError('webhook base URL must use https')
    if not parsed.hostname:
        raise ValueError('webhook base URL must include a hostname')
    hostname = parsed.hostname.lower()
    if hostname in {'localhost', '0.0.0.0'} or hostname.endswith('.localhost') or hostname.endswith('.local'):
        raise ValueError('webhook base URL must not be localhost')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified:
        raise ValueError('webhook base URL must be publicly routable')


def install_webhooks(
    repos: list[str],
    *,
    base_url: str,
    template_id: str,
    secret: str,
    events: list[str] | None = None,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> list[WebhookTarget]:
    """Create or update GitHub repository webhooks for a deployed template."""

    if not secret:
        raise ValueError('GitHub webhook secret is required')
    hook_url = webhook_url(base_url, template_id)
    event_names = events or ['push', 'pull_request']
    results: list[WebhookTarget] = []
    for repo in repos:
        _validate_repo(repo)
        existing = _matching_hook(repo, hook_url, runner=runner)
        action = 'update' if existing else 'create'
        hook_id = _hook_id(existing)
        if not dry_run:
            payload = {
                'name': 'web',
                'active': True,
                'events': event_names,
                'config': {
                    'url': hook_url,
                    'content_type': 'json',
                    'secret': secret,
                    'insecure_ssl': '0',
                },
            }
            response = _gh_json(
                ['repos', repo, 'hooks', str(hook_id)] if existing else ['repos', repo, 'hooks'],
                runner=runner,
                method='PATCH' if existing else 'POST',
                payload=payload,
            )
            hook_id = _hook_id(response)
        results.append(WebhookTarget(repo=repo, hook_id=hook_id, action=action, url=hook_url))
    return results


def _validate_repo(repo: str) -> None:
    parts = repo.split('/')
    if len(parts) != 2 or not all(parts):
        raise ValueError(f'invalid GitHub repo: {repo}')


def _matching_hook(repo: str, url: str, *, runner: Runner) -> dict[str, Any] | None:
    hooks = _gh_json(['repos', repo, 'hooks'], runner=runner)
    for hook in hooks if isinstance(hooks, list) else []:
        config = hook.get('config') if isinstance(hook, dict) else None
        if isinstance(config, dict) and config.get('url') == url:
            return hook
    return None


def _hook_id(hook: dict[str, Any] | None) -> int | None:
    if not hook:
        return None
    value = hook.get('id')
    return int(value) if value is not None else None


def _gh_json(
    path: list[str],
    *,
    runner: Runner,
    method: str = 'GET',
    payload: dict[str, Any] | None = None,
) -> Any:
    args = ['gh', 'api', '/'.join(path), '-X', method]
    stdin = None
    if payload is not None:
        args.extend(['-H', 'Content-Type: application/json', '--input', '-'])
        stdin = json.dumps(payload)
    completed = runner(args, input=stdin, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f'gh api failed for {"/".join(path)}'
        raise RuntimeError(message)
    if not completed.stdout.strip():
        return {}
    return json.loads(completed.stdout)
