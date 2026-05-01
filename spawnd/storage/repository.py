"""Transactional repository seams for compound DB operations."""

import sqlite3
from contextlib import contextmanager
from typing import Generator


@contextmanager
def transaction(db: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Run a group of DB operations atomically."""
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
