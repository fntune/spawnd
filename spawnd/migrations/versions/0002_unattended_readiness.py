"""Add unattended run templates, schedules, and write capability state.

Revision ID: 0002_unattended_readiness
Revises: 0001_deployed_backend
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002_unattended_readiness'
down_revision = '0001_deployed_backend'
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), 'postgresql')


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    return column in {row['name'] for row in sa.inspect(op.get_bind()).get_columns(table)}


def _index_exists(table: str, index: str) -> bool:
    if not _table_exists(table):
        return False
    return index in {row['name'] for row in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if not _table_exists('run_templates'):
        op.create_table(
            'run_templates',
            sa.Column('id', sa.String(160), primary_key=True),
            sa.Column('name', sa.String(240), nullable=False),
            sa.Column('description', sa.Text()),
            sa.Column('plan_template', sa.Text(), nullable=False),
            sa.Column('source_repo_template', sa.Text()),
            sa.Column('source_ref_template', sa.Text()),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                'updated_at',
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if not _table_exists('schedules'):
        op.create_table(
            'schedules',
            sa.Column('id', sa.String(160), primary_key=True),
            sa.Column('template_id', sa.String(160), sa.ForeignKey('run_templates.id', ondelete='CASCADE'), nullable=False),
            sa.Column('name', sa.String(240), nullable=False),
            sa.Column('status', sa.String(40), nullable=False, server_default='active'),
            sa.Column('interval_seconds', sa.Integer(), nullable=False),
            sa.Column('parameters', json_type, nullable=False),
            sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('last_run_at', sa.DateTime(timezone=True)),
            sa.Column('last_run_id', sa.String(160)),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column(
                'updated_at',
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if _table_exists('agents') and not _column_exists('agents', 'write_allowed'):
        op.add_column(
            'agents',
            sa.Column('write_allowed', sa.Boolean(), nullable=False, server_default='true'),
        )

    if _table_exists('schedules') and not _index_exists('schedules', 'idx_schedules_status_next_run'):
        op.create_index('idx_schedules_status_next_run', 'schedules', ['status', 'next_run_at'])


def downgrade() -> None:
    if _table_exists('schedules') and _index_exists('schedules', 'idx_schedules_status_next_run'):
        op.drop_index('idx_schedules_status_next_run', table_name='schedules')
    if _table_exists('agents') and _column_exists('agents', 'write_allowed'):
        op.drop_column('agents', 'write_allowed')
    if _table_exists('schedules'):
        op.drop_table('schedules')
    if _table_exists('run_templates'):
        op.drop_table('run_templates')
