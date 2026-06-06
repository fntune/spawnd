"""Create greenfield deployed spawnd schema.

Revision ID: 0001_deployed_backend
Revises:
Create Date: 2026-06-04
"""
from __future__ import annotations

from alembic import op

from spawnd.state.schema import metadata

revision = '0001_deployed_backend'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
