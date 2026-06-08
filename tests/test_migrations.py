"""Alembic migration contract tests."""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from spawnd.state import schema


def test_version_migrations_do_not_import_live_state_metadata():
    versions = Path('spawnd/migrations/versions')
    for path in versions.glob('*.py'):
        text = path.read_text()
        assert 'spawnd.state.schema' not in text
        if path.name != '0001_deployed_backend.py':
            assert 'metadata.create_all' not in text


def test_alembic_head_matches_state_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / 'spawnd.sqlite'
    url = f'sqlite:///{db_path}'
    monkeypatch.setenv('SPAWND_DATABASE_URL', url)

    command.upgrade(Config('alembic.ini'), 'head')

    engine = sa.create_engine(url)
    inspector = sa.inspect(engine)
    actual_tables = set(inspector.get_table_names()) - {'alembic_version'}
    assert actual_tables == set(schema.metadata.tables)

    for table in schema.metadata.tables.values():
        actual_columns = {column['name'] for column in inspector.get_columns(table.name)}
        expected_columns = {column.name for column in table.columns}
        assert actual_columns == expected_columns

        actual_indexes = {index['name'] for index in inspector.get_indexes(table.name)}
        expected_indexes = {index.name for index in table.indexes if index.name}
        assert expected_indexes <= actual_indexes
