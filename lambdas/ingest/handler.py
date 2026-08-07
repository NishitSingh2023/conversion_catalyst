"""Ingest stage: open a pipeline run and validate the incoming lead batch.

Step Functions entry point. Lead data already lives in Postgres (written by
Airbyte/Snowflake in production, by the loader script in the hackathon), so this
stage moves no data. Its job is to:

    1. Allocate a ``run_id`` and open the ``pipeline_runs`` audit row.
    2. Resolve which ``batch_id`` to process (explicit, or the newest batch).
    3. Mark each lead valid or invalid, persisting the verdict.
    4. Refresh derived manager profiles so later stages see current data.

Validation writes ``new_leads.is_valid`` rather than only counting problems.
Every downstream stage filters on that flag, so an unusable lead is excluded
rather than silently mis-scored - a lead with an unrecognised intent bucket would
otherwise reach the model with every intent one-hot at zero, which the model
reads as "no intent signal" instead of "bad data".

Event shape (all optional)::

    {"batch_id": "2026-08-07", "run_id": "run-...", "business_date": "2026-08-07"}
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from shared.constants import INTENT_BUCKETS
from shared.db import get_engine, read_sql
from shared.manager_profiles import refresh_manager_profiles
from shared.pipeline import fail_run, new_run_id, start_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Fields that must be populated for a lead to be scoreable downstream.
REQUIRED_FIELDS = ("intent_bucket", "geography", "language", "product_interest")

# Recompute validity in SQL so the verdict is stored next to the data and the
# rule cannot drift between a Python copy and the queries that rely on it.
_VALIDATE = """
UPDATE new_leads SET
    validation_error = CASE
        {missing_cases}
        WHEN intent_bucket <> ALL(:buckets) THEN 'invalid_intent_bucket'
    END
WHERE batch_id = :batch_id
"""

_APPLY_FLAG = """
UPDATE new_leads
SET is_valid = (validation_error IS NULL)
WHERE batch_id = :batch_id
"""


def _validate_sql() -> str:
    # One WHEN per required field, checking null and blank in the same pass.
    cases = "\n        ".join(
        f"WHEN {f} IS NULL OR btrim({f}) = '' THEN 'missing_{f}'" for f in REQUIRED_FIELDS
    )
    return _VALIDATE.format(missing_cases=cases)


def _latest_batch_id() -> str | None:
    df = read_sql(
        """
        SELECT batch_id
        FROM new_leads
        WHERE batch_id IS NOT NULL
        GROUP BY batch_id
        ORDER BY max(created_at) DESC
        LIMIT 1
        """
    )
    return None if df.empty else str(df.iloc[0]["batch_id"])


def validate_batch(batch_id: str) -> dict:
    """Flag valid/invalid leads in a batch and summarise the reasons."""
    with get_engine().begin() as conn:
        conn.execute(
            text(_validate_sql()),
            {"batch_id": batch_id, "buckets": list(INTENT_BUCKETS)},
        )
        conn.execute(text(_APPLY_FLAG), {"batch_id": batch_id})

    breakdown = read_sql(
        """
        SELECT COALESCE(validation_error, '_valid') AS reason, count(*) AS n
        FROM new_leads WHERE batch_id = :batch_id
        GROUP BY 1
        """,
        {"batch_id": batch_id},
    )
    counts = dict(zip(breakdown["reason"], breakdown["n"].astype(int), strict=False))
    valid = counts.pop("_valid", 0)
    return {
        "total": valid + sum(counts.values()),
        "valid": int(valid),
        "invalid": int(sum(counts.values())),
        "invalid_reasons": counts,
    }


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id") or new_run_id()
    batch_id = event.get("batch_id") or _latest_batch_id()

    start_run(run_id, batch_id=batch_id)

    try:
        if not batch_id:
            raise RuntimeError("no batch_id supplied and new_leads has no batches to process")

        stats = validate_batch(batch_id)
        if stats["valid"] == 0:
            raise RuntimeError(f"batch {batch_id} has no valid leads (stats={stats})")

        # Manager attributes are derived from history, so refresh before the
        # eligibility and scoring stages read them.
        profiles_written = refresh_manager_profiles()

        # Business date owns the capacity window for every later stage. Pinned
        # here so a retry crossing midnight UTC still counts against this run's
        # day rather than resetting every manager's load to zero.
        business_date = event.get("business_date") or str(
            read_sql("SELECT current_date AS d").iloc[0]["d"]
        )

        update_run(
            run_id,
            stage="ingest",
            leads_processed=stats["valid"],
            errors=str(stats["invalid_reasons"]) if stats["invalid_reasons"] else None,
        )

        logger.info(
            "ingest run=%s batch=%s date=%s valid=%s invalid=%s reasons=%s profiles=%s",
            run_id, batch_id, business_date, stats["valid"], stats["invalid"],
            stats["invalid_reasons"], profiles_written,
        )

        return {
            "run_id": run_id,
            "batch_id": batch_id,
            "business_date": business_date,
            "leads_valid": stats["valid"],
            "leads_invalid": stats["invalid"],
            "invalid_reasons": stats["invalid_reasons"],
            "manager_profiles": profiles_written,
        }
    except Exception as exc:
        logger.exception("ingest failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="ingest")
        raise
