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

    {"batch_id": "2026-08-07", "run_id": "run-...", "business_date": "2026-08-07",
     "reset_business_date": false}

``reset_business_date`` is a demo-only escape hatch - see
:func:`reset_business_date`. It defaults to off and is never applied implicitly.
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

# --- Business-date reset (demo only) --------------------------------------
# Pool rows carry no business_date of their own, so they are scoped two ways:
# a claimed row consumes capacity on claimed_at::date (exactly how
# manager_daily_load counts it), and the remaining rows belong to a run whose
# assignments are dated to the target day. Both are restricted to the one date.
_RESET_POOL = """
DELETE FROM pool p
WHERE p.claimed_at::date = :business_date
   OR p.run_id IN (
        SELECT run_id FROM assignments WHERE business_date = :business_date
      )
"""

_RESET_ASSIGNMENTS = """
DELETE FROM assignments WHERE business_date = :business_date
"""


def _validate_sql() -> str:
    # One WHEN per required field, checking null and blank in the same pass.
    cases = "\n        ".join(
        f"WHEN {f} IS NULL OR btrim({f}) = '' THEN 'missing_{f}'" for f in REQUIRED_FIELDS
    )
    return _VALIDATE.format(missing_cases=cases)


def reset_business_date(business_date: str) -> dict:
    """Delete every assignment and pool row for ``business_date``. DESTRUCTIVE.

    Capacity is keyed on the business date, so a second run on the same date
    correctly sees the first run's 471 assignments as load already held and
    places fewer leads - by the third run a chunk of the team sits at the
    50-lead cap. That is right for a nightly job and wrong for a demo, where a
    re-run should reproduce the same numbers.

    This exists ONLY to make a demo repeatable and MUST NOT be used in
    production: it erases the audit trail of what was already assigned today,
    and any of those leads already pushed to the CRM stay assigned there while
    the record here is gone. It is opt-in per invocation
    (``{"reset_business_date": true}``), never implicit, and touches no other
    date's data.
    """
    with get_engine().begin() as conn:
        # Pool first: its scoping reads the assignments rows deleted below.
        pool_deleted = conn.execute(text(_RESET_POOL), {"business_date": business_date}).rowcount
        assignments_deleted = conn.execute(
            text(_RESET_ASSIGNMENTS), {"business_date": business_date}
        ).rowcount

    logger.warning(
        "reset_business_date=%s DELETED assignments=%s pool=%s "
        "(destructive, demo-only capacity reset)",
        business_date, assignments_deleted, pool_deleted,
    )
    return {"assignments_deleted": int(assignments_deleted), "pool_deleted": int(pool_deleted)}


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

        # Business date owns the capacity window for every later stage. Pinned
        # here so a retry crossing midnight UTC still counts against this run's
        # day rather than resetting every manager's load to zero.
        business_date = event.get("business_date") or str(
            read_sql("SELECT current_date AS d").iloc[0]["d"]
        )

        # Opt-in, destructive, demo-only: clear this date's capacity window so a
        # re-run reproduces the first run's numbers. Off unless the event asks.
        reset_stats = (
            reset_business_date(business_date)
            if event.get("reset_business_date") is True
            else None
        )

        stats = validate_batch(batch_id)
        if stats["valid"] == 0:
            raise RuntimeError(f"batch {batch_id} has no valid leads (stats={stats})")

        # Manager attributes are derived from history, so refresh before the
        # eligibility and scoring stages read them.
        profiles_written = refresh_manager_profiles()

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
            **({"reset": reset_stats} if reset_stats else {}),
        }
    except Exception as exc:
        logger.exception("ingest failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="ingest")
        raise
