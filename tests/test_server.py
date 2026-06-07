"""Tests for the deployed HTTP API boundary."""
from __future__ import annotations

from fastapi.testclient import TestClient

from spawnd import server
from spawnd.coordination.redis import InMemoryCoordinator
from tests.deployed_helpers import make_repo


def test_http_submit_validates_plan(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    client = TestClient(server.create_app())

    response = client.post(
        '/runs',
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
    client = TestClient(server.create_app())

    response = client.post('/runs', json={'plan_file': '/tmp/plan.yaml'})

    assert response.status_code == 422
    assert repo.list_runs() == []


def test_http_submit_accepts_serialized_plan(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    monkeypatch.setattr(server, '_repository', lambda: repo)
    monkeypatch.setattr(server, '_coordinator', lambda: coordinator)
    client = TestClient(server.create_app())

    response = client.post(
        '/runs',
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
