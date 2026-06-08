"""Tests for the deployed HTTP API boundary."""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from spawnd import server
from spawnd.coordination.redis import InMemoryCoordinator
from tests.deployed_helpers import make_repo


AUTH = {'Authorization': 'Bearer test-token'}


def test_health_metrics_are_available_without_auth(monkeypatch):
    monkeypatch.delenv('SPAWND_API_TOKEN', raising=False)
    client = TestClient(server.create_app())

    assert client.get('/healthz').status_code == 200
    assert client.get('/readyz').status_code == 200
    metrics = client.get('/metrics')
    assert metrics.status_code == 200
    assert 'spawnd_backend_configured' in metrics.text


def test_http_api_requires_bearer_token(monkeypatch):
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())

    assert client.get('/runs/run-1').status_code == 401
    assert client.get('/runs/run-1', headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_http_submit_validates_plan(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())

    response = client.post(
        '/runs',
        headers=AUTH,
        json={
            'run_id': 'run-1',
            'plan': {
                'name': 'bad-plan',
                'agents': [{'name': 'a', 'prompt': 'task', 'depends_on': ['missing']}],
            },
        },
    )

    assert response.status_code == 422
    assert response.json()['detail'] == ['Agent a depends on unknown agent: missing']
    assert repo.get_run('run-1') is None


def test_http_submit_rejects_server_local_plan_file(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())

    response = client.post('/runs', headers=AUTH, json={'plan_file': '/tmp/plan.yaml'})

    assert response.status_code == 422
    assert repo.list_runs() == []


def test_http_submit_accepts_serialized_plan(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())

    response = client.post(
        '/runs',
        headers=AUTH,
        json={
            'run_id': 'run-1',
            'source_repo': '/repo',
            'source_ref': 'origin/main',
            'plan': {'name': 'good-plan', 'agents': [{'name': 'a', 'prompt': 'task'}]},
        },
    )

    assert response.status_code == 200
    assert response.json() == {'run_id': 'run-1'}
    assert repo.get_run('run-1')['source_repo'] == '/repo'
    assert [job.agent for job in coordinator.jobs] == ['a']


def test_http_submission_queue_enqueue_and_drain(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())

    response = client.post(
        '/submissions',
        headers=AUTH,
        json={
            'kind': 'plan',
            'run_id': 'queued-run',
            'source_repo': '/repo',
            'plan': {'name': 'queued', 'agents': [{'name': 'a', 'prompt': 'task'}]},
        },
    )

    assert response.status_code == 200
    assert response.json() == {'status': 'queued'}
    assert coordinator.submission_queue_depth() == 1

    response = client.post('/submissions/drain', headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {'status': 'submitted', 'run_id': 'queued-run'}
    assert repo.get_run('queued-run')['source_repo'] == '/repo'


def test_http_template_submit_and_due_schedule_use_deployed_submission(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    client = TestClient(server.create_app())
    plan_template = """
name: "{name}"
agents:
  - name: contributor
    prompt: "Improve {repo}"
"""

    assert client.post(
        '/templates',
        headers=AUTH,
        json={
            'id': 'contributor',
            'name': 'Contributor',
            'plan_template': plan_template,
            'source_repo_template': '{clone_url}',
            'source_ref_template': '{ref}',
        },
    ).status_code == 200

    response = client.post(
        '/templates/contributor/runs',
        headers=AUTH,
        json={
            'run_id': 'run-template-1',
            'parameters': {
                'name': 'templated',
                'repo': 'acme/app',
                'clone_url': 'https://github.com/acme/app.git',
                'ref': 'main',
            },
        },
    )
    assert response.status_code == 200
    assert repo.get_run('run-template-1')['source_repo'] == 'https://github.com/acme/app.git'
    assert [job.agent for job in coordinator.jobs] == ['contributor']

    response = client.post(
        '/schedules',
        headers=AUTH,
        json={
            'id': 'schedule-1',
            'template_id': 'contributor',
            'name': 'Nightly contributor',
            'interval_seconds': 60,
            'parameters': {
                'name': 'scheduled',
                'repo': 'acme/app',
                'clone_url': 'https://github.com/acme/app.git',
                'ref': 'main',
            },
        },
    )
    assert response.status_code == 200

    response = client.post('/schedules/run-due', headers=AUTH)
    assert response.status_code == 200
    assert response.json()[0]['schedule_id'] == 'schedule-1'


def test_github_webhook_requires_valid_signature_and_submits_template(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    monkeypatch.setenv('SPAWND_API_TOKEN', 'test-token')
    monkeypatch.setenv('SPAWND_GITHUB_WEBHOOK_SECRET', 'webhook-secret')
    client = TestClient(server.create_app())
    plan_template = """
name: "github-{event}"
agents:
  - name: contributor
    prompt: "Review {repo} at {after}"
"""
    client.post(
        '/templates',
        headers=AUTH,
        json={
            'id': 'github-contributor',
            'name': 'GitHub Contributor',
            'plan_template': plan_template,
            'source_repo_template': '{clone_url}',
            'source_ref_template': '{after}',
        },
    )
    payload = {
        'ref': 'refs/heads/main',
        'after': 'abc123',
        'before': 'def456',
        'repository': {
            'full_name': 'acme/app',
            'clone_url': 'https://github.com/acme/app.git',
            'ssh_url': 'git@github.com:acme/app.git',
            'default_branch': 'main',
        },
    }
    body = json.dumps(payload).encode('utf-8')
    signature = 'sha256=' + hmac.new(b'webhook-secret', body, hashlib.sha256).hexdigest()

    rejected = client.post(
        '/webhooks/github/github-contributor',
        content=body,
        headers={'X-GitHub-Event': 'push', 'X-Hub-Signature-256': 'sha256=bad'},
    )
    assert rejected.status_code == 401

    accepted = client.post(
        '/webhooks/github/github-contributor',
        content=body,
        headers={'X-GitHub-Event': 'push', 'X-Hub-Signature-256': signature, 'Content-Type': 'application/json'},
    )
    assert accepted.status_code == 200
    run = repo.get_run(accepted.json()['run_id'])
    assert run is not None
    assert run['source_repo'] == 'https://github.com/acme/app.git'
    assert run['source_ref'] == 'abc123'
