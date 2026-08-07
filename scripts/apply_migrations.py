#!/usr/bin/env python
"""Apply SQL migrations in db_migrations/ against the configured Postgres.

Migrations are applied in filename order and tracked in a schema_migrations
table so re-running is idempotent.

Usage:
    python scripts/apply_migrations.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from shared.db import get_engine  # noqa: E402

MIGRATIONS_DIR = REPO_ROOT / "db_migrations"

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def applied_migrations(conn) -> set[str]:
    rows = conn.execute(text("SELECT filename FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    engine = get_engine()
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        print(f"No migrations found in {MIGRATIONS_DIR}")
        return 0

    with engine.begin() as conn:
        conn.execute(text(TRACKING_DDL))
        done = applied_migrations(conn)

    for path in migrations:
        if path.name in done:
            print(f"skip  {path.name} (already applied)")
            continue
        sql = path.read_text()
        print(f"apply {path.name} ...")
        with engine.begin() as conn:
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )
    print("Migrations complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
