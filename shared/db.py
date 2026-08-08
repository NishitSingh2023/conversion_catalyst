"""Database access helpers built on SQLAlchemy.

A single engine is cached per process (Lambda container) so repeated invocations
reuse the connection pool. All modules should obtain sessions/connections through
this module rather than constructing their own engines.
"""
from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator, Sequence
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


# --- COPY bulk load -------------------------------------------------------
# `write_dataframe` is fine for the per-run tables (tens of thousands of rows) but
# not for loading history: 2.7M rows at 5,000 per INSERT is 544 statements, each a
# full round trip, which is slow locally and painful over an SSH tunnel to RDS.
# COPY sends the same rows as one stream per chunk and lets Postgres parse them,
# which is roughly an order of magnitude faster and does not grow the statement.
#
# Rows per COPY buffer. The buffer is materialised as CSV text in memory, so this
# trades round trips against transient memory: 50k rows of a ~14-column frame is a
# few tens of MB.
COPY_BUFFER_ROWS = 50_000

# Table/column names are interpolated into the COPY statement (they cannot be
# bound as parameters), so they are validated as plain identifiers first.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _checked_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"refusing to interpolate unsafe SQL identifier: {name!r}")
    return name


def _csv_buffer(df: pd.DataFrame) -> io.StringIO:
    """Render a frame as the CSV payload COPY expects.

    Two details matter for correctness:

    * **NULLs.** In ``FORMAT csv`` an *unquoted* empty field is NULL while a
      quoted one is the empty string. pandas writes NaN/None as an unquoted empty
      field, so missing values land as SQL NULL - which is what the adapter means
      by ``None`` (it never emits an empty string; ``_clean_str`` turns blanks
      into ``None``).
    * **Booleans.** pandas renders them ``True``/``False``; Postgres accepts that,
      but only for a real bool dtype. An object column holding Python bools would
      render the same way, so both are mapped explicitly to ``t``/``f``.
    """
    frame = df.copy()
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_bool_dtype(series):
            frame[column] = series.map({True: "t", False: "f"})
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, header=False, na_rep="")
    buffer.seek(0)
    return buffer


def copy_dataframes(
    chunks: Iterable[pd.DataFrame],
    table: str,
    columns: Sequence[str] | None = None,
    truncate: bool = False,
    buffer_rows: int = COPY_BUFFER_ROWS,
) -> int:
    """Stream frames into ``table`` with COPY. Returns the rows written.

    ``chunks`` is consumed lazily, so a caller can hand over a generator that
    reads and adapts one CSV chunk at a time and never holds the whole file. The
    truncate and every COPY share one transaction: either the table ends up fully
    reloaded or untouched.
    """
    table = _checked_identifier(table)
    raw = get_engine().raw_connection()
    written = 0
    try:
        cursor = raw.cursor()
        if truncate:
            cursor.execute(f"TRUNCATE {table} RESTART IDENTITY")
        for chunk in chunks:
            if chunk.empty:
                continue
            names = [_checked_identifier(c) for c in (columns or chunk.columns)]
            statement = (
                f"COPY {table} ({', '.join(names)}) FROM STDIN WITH (FORMAT csv)"
            )
            for start in range(0, len(chunk), buffer_rows):
                part = chunk.iloc[start : start + buffer_rows][names]
                cursor.copy_expert(statement, _csv_buffer(part))
                written += len(part)
        cursor.close()
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return written


def copy_dataframe(
    df: pd.DataFrame,
    table: str,
    columns: Sequence[str] | None = None,
    truncate: bool = False,
) -> int:
    """Single-frame convenience wrapper around :func:`copy_dataframes`."""
    return copy_dataframes([df], table, columns=columns, truncate=truncate)


def execute(statement: str, params: dict | None = None) -> None:
    """Execute a single write/DDL statement."""
    with get_engine().begin() as conn:
        conn.execute(text(statement), params or {})
