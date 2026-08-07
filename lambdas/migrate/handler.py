"""Apply SQL migrations from inside the VPC.

RDS sits in private subnets with no public access and no bastion, so migrations
cannot be applied from a laptop. This function ships in the same image (which
carries db_migrations/) and runs inside the VPC, making it the supported path
for creating and upgrading the schema after ``terraform apply``.

Invoke it once after deploy, and again after adding a migration file:

    aws lambda invoke --function-name lead-assignment-dev-migrate /dev/stdout

Applied files are tracked in ``schema_migrations`` so repeat invocations are
no-ops. This mirrors scripts/apply_migrations.py, which remains the local path.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import text

from shared.db import get_engine

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Baked into the image at LAMBDA_TASK_ROOT by the Dockerfile.
MIGRATIONS_DIR = Path(os.getenv("MIGRATIONS_DIR", "/var/task/db_migrations"))

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def lambda_handler(event: dict | None = None, context=None) -> dict:
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"no .sql files found in {MIGRATIONS_DIR}")

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(TRACKING_DDL))
        already = {r[0] for r in conn.execute(text("SELECT filename FROM schema_migrations"))}

    applied, skipped = [], []
    for path in migrations:
        if path.name in already:
            skipped.append(path.name)
            continue
        logger.info("applying migration %s", path.name)
        # Each migration and its bookkeeping commit together, so a failure
        # part-way through leaves earlier migrations recorded and this one not.
        with engine.begin() as conn:
            conn.execute(text(path.read_text()))
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )
        applied.append(path.name)

    logger.info("migrations applied=%s skipped=%s", applied, skipped)
    return {"applied": applied, "skipped": skipped}
