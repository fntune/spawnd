"""Helpers for deployed-state integration tests."""
from __future__ import annotations

import os

import pytest

from spawnd.state import schema
from spawnd.state.repository import DeployedRepository


def make_repo() -> DeployedRepository:
    url = os.environ.get("SPAWND_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SPAWND_TEST_DATABASE_URL is required for deployed state tests")
    repo = DeployedRepository.from_url(url)
    schema.metadata.drop_all(repo.engine)
    repo.create_schema()
    return repo
