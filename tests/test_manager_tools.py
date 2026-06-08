"""Tests for deployed manager coordination tools."""
from __future__ import annotations

import pytest

from spawnd.coordination.redis import InMemoryCoordinator
from spawnd.models.specs import AgentSpec, ManagerSettings, PlanSpec
from spawnd.tools import manager
from tests.deployed_helpers import make_repo


@pytest.mark.asyncio
async def test_spawn_worker_tool_creates_agent_and_enqueues(monkeypatch):
    repo = make_repo()
    coordinator = InMemoryCoordinator()
    plan = PlanSpec(
        name='managed',
        agents=[
            AgentSpec(
                name='manager',
                type='manager',
                prompt='manage',
                manager=ManagerSettings(max_subagents=2),
            )
        ],
    )
    repo.create_run(plan, 'run-1')
    monkeypatch.setattr(manager, '_repository', lambda: repo)
    monkeypatch.setattr(manager, '_coordinator', lambda: coordinator)

    message = await manager.spawn_worker('run-1', 'manager', 'worker', 'do worker task', check='true')

    assert message == 'Spawned worker: manager.worker'
    assert repo.get_agent('run-1', 'manager.worker')['status'] == 'queued'
    assert [job.agent for job in coordinator.jobs] == ['manager.worker']
