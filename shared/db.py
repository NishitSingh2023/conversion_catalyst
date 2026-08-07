"""Database access helpers built on SQLAlchemy.

A single engine is cached per process (Lambda container) so repeated invocations
reuse the connection pool. All modules should obtain sessions/connections through
this module rather than constructing their own engines.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import pandas as pd
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_config


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine for the configured database."""
    cfg = get_config()
    return create_engine(
        cfg.database.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def read_sql(query: str, params: dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame. Convenience for read-heavy code."""
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params or {})


# Rows per INSERT statement. Unbounded `method="multi"` builds a single
# statement covering every row: measured at 1.0GB resident memory and 24s for
# 225k rows, and Postgres also caps a statement at 65535 bind parameters.
# Chunking keeps memory flat and stays well inside that ceiling.
WRITE_CHUNK_SIZE = 5_000


def write_dataframe(
    df: pd.DataFrame,
    table: str,
    if_exists: str = "append",
    chunksize: int = WRITE_CHUNK_SIZE,
) -> int:
    """Bulk-insert a DataFrame into a table. Returns the number of rows written.

    Note ``if_exists="append"`` will create a missing table with pandas-inferred
    types and no constraints. Tables are expected to already exist via
    db_migrations/; prefer running migrations over relying on auto-creation.
    """
    if df.empty:
        return 0
    with get_engine().begin() as conn:
        df.to_sql(
            table,
            conn,
            if_exists=if_exists,
            index=False,
            method="multi",
            chunksize=chunksize,
        )
    return len(df)


def execute(statement: str, params: dict | None = None) -> None:
    """Execute a single write/DDL statement."""
    with get_engine().begin() as conn:
        conn.execute(text(statement), params or {})
