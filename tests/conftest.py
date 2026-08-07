"""Shared pytest fixtures.

Puts the repo root on ``sys.path`` so ``import shared`` works regardless of where
pytest is invoked, and provisions an isolated test database for the tests that
exercise SQL.

Why a separate database: the eligibility rules are implemented as SQL because the
candidate set is |leads| x |managers| (3M rows at target scale), so they can only
be tested against a real Postgres. Running against the development database made
assertions depend on whatever data happened to be loaded - the handler correctly
considers every manager in ``manager_profiles``, so exact counts were polluted by
the 80 sample managers. Tests now own their own database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_DB_NAME = os.getenv("TEST_DB_NAME", "lead_assignment_test")


def _reset_shared_caches() -> None:
    """Drop cached config/engine so a new DB_NAME takes effect."""
    from shared import config, db

    config.get_config.cache_clear()
    db.get_engine.cache_clear()
    db._session_factory.cache_clear()


def _provision_test_database() -> None:
    """Create the test database if absent, then apply all migrations to it."""
    from sqlalchemy import create_engine, text

    from shared.config import get_config

    cfg = get_config()
    # Connect to the maintenance database to issue CREATE DATABASE.
    admin_url = (
        f"postgresql+psycopg2://{cfg.database.user}:{cfg.database.password}"
        f"@{cfg.database.host}:{cfg.database.port}/postgres"
    )
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB_NAME}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()

    from sqlalchemy import text as sqltext

    from shared.db import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sqltext(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        applied = {r[0] for r in conn.execute(sqltext("SELECT filename FROM schema_migrations"))}

    for path in sorted((REPO_ROOT / "db_migrations").glob("*.sql")):
        if path.name in applied:
            continue
        with engine.begin() as conn:
            conn.execute(sqltext(path.read_text()))
            conn.execute(
                sqltext("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )


@pytest.fixture(scope="session")
def db():
    """An isolated, migrated test database. Skips if Postgres is unreachable.

        docker compose up -d postgres
        DB_PORT=5433 pytest
    """
    os.environ["DB_NAME"] = TEST_DB_NAME
    _reset_shared_caches()

    try:
        _provision_test_database()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")

    from shared.db import get_engine

    engine = get_engine()
    yield engine
    engine.dispose()
