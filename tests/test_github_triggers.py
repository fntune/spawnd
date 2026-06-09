"""Tests for GitHub trigger installation."""
from __future__ import annotations

import json
import subprocess

import pytest

from spawnd.triggers.github import install_webhooks, webhook_url


def test_webhook_url_rejects_non_public_targets():
    for url in ['http://spawnd.example.com', 'https://localhost:8765', 'https://127.0.0.1:8765']:
        with pytest.raises(ValueError):
            webhook_url(url, 'github-contributor')


def test_install_webhooks_creates_and_updates_without_secret_in_argv():
    base_url = 'https://spawnd.example.com'
    target_url = f'{base_url}/webhooks/github/github-contributor'
    calls: list[tuple[list[str], str | None]] = []

    def runner(args, *, input, capture_output, text, check):
        _ = capture_output
        _ = text
        _ = check
        calls.append((args, input))
        endpoint = args[2]
        method = args[4]
        if endpoint == 'repos/acme/new/hooks' and method == 'GET':
            return subprocess.CompletedProcess(args, 0, '[]', '')
        if endpoint == 'repos/acme/new/hooks' and method == 'POST':
            return subprocess.CompletedProcess(args, 0, '{"id": 12}', '')
        if endpoint == 'repos/acme/existing/hooks' and method == 'GET':
            return subprocess.CompletedProcess(args, 0, json.dumps([{'id': 34, 'config': {'url': target_url}}]), '')
        if endpoint == 'repos/acme/existing/hooks/34' and method == 'PATCH':
            return subprocess.CompletedProcess(args, 0, '{"id": 34}', '')
        raise AssertionError(args)

    results = install_webhooks(
        ['acme/new', 'acme/existing'],
        base_url=base_url,
        template_id='github-contributor',
        secret='top-secret',
        runner=runner,
    )

    assert [(item.repo, item.action, item.hook_id) for item in results] == [
        ('acme/new', 'create', 12),
        ('acme/existing', 'update', 34),
    ]
    assert all('top-secret' not in part for args, _ in calls for part in args)
    write_payloads = [json.loads(stdin) for _, stdin in calls if stdin]
    assert [payload['config']['secret'] for payload in write_payloads] == ['top-secret', 'top-secret']
    assert [payload['config']['url'] for payload in write_payloads] == [target_url, target_url]


def test_install_webhooks_dry_run_does_not_write():
    def runner(args, *, input, capture_output, text, check):
        _ = input
        _ = capture_output
        _ = text
        _ = check
        assert args[4] == 'GET'
        return subprocess.CompletedProcess(args, 0, '[]', '')

    results = install_webhooks(
        ['acme/app'],
        base_url='https://spawnd.example.com',
        template_id='github-contributor',
        secret='top-secret',
        dry_run=True,
        runner=runner,
    )

    assert [(item.repo, item.action, item.hook_id) for item in results] == [('acme/app', 'create', None)]
