"""Ingest stage: open a pipeline run and validate the incoming lead batch.

This is the Step Functions entry point. It does not move lead data around -
leads already live in Postgres (written by Airbyte/Snowflake in production, by
the loader script in the hackathon). Its job is to:

    1. Allocate a ``run_id`` and open the ``pipeline_runs`` audit row.
    2. Resolve which ``batch_id`` to process (explicit, or the newest batch).
    3. Validate the batch and report how many leads are usable.
    4. Refresh the derived manager profiles so later stages see current data.

Event shape (all optional)::

    {"batch_id": "2026-08-07", "run_id": "run-..."}

Returns the run context consumed by the eligibility stage.
"""
from __future__ import annotations

import logging

from shared.constants import INTENT_BUCKETS
from shared.db import read_sql
from shared.manager_profiles import refresh_manager_profiles
from shared.pipeline import fail_run, new_run_id, start_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Columns a lead must have populated to be scoreable downstream.
REQUIRED_FIELDS = ("lead_id", "intent_bucket", "geography", "language", "product_interest")


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
    """Count valid vs invalid leads in a batch.

    A lead is invalid if a required field is null/blank or its intent bucket is
    not one the upstream classifier is supposed to emit. Invalid leads are
    excluded from assignment rather than silently mis-scored.
    """
    leads = read_sql(
        "SELECT * FROM new_leads WHERE batch_id = :batch_id",
        {"batch_id": batch_id},
    )
    if leads.empty:
        return {"total": 0, "valid": 0, "invalid": 0, "invalid_reasons": {}}

    reasons: dict[str, int] = {}
    invalid_mask = leads["lead_id"].isna()  # all-False seed of the right shape

    for field in REQUIRED_FIELDS:
        missing = leads[field].isna() | (leads[field].astype(str).str.strip() == "")
        if missing.any():
            reasons[f"missing_{field}"] = int(missing.sum())
        invalid_mask = invalid_mask | missing

    bad_bucket = ~leads["intent_bucket"].isin(INTENT_BUCKETS)
    if bad_bucket.any():
        reasons["invalid_intent_bucket"] = int(bad_bucket.sum())
    invalid_mask = invalid_mask | bad_bucket

    return {
        "total": int(len(leads)),
        "valid": int((~invalid_mask).sum()),
        "invalid": int(invalid_mask.sum()),
        "invalid_reasons": reasons,
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

        # Manager attributes are derived from history, so refresh them before
        # eligibility and scoring read them.
        profiles_written = refresh_manager_profiles()

        update_run(
            run_id,
            stage="ingest",
            leads_processed=stats["valid"],
            errors=None if not stats["invalid_reasons"] else str(stats["invalid_reasons"]),
        )

        logger.info(
            "ingest run=%s batch=%s valid=%s invalid=%s profiles=%s",
            run_id, batch_id, stats["valid"], stats["invalid"], profiles_written,
        )

        return {
            "run_id": run_id,
            "batch_id": batch_id,
            "leads_valid": stats["valid"],
            "leads_invalid": stats["invalid"],
            "invalid_reasons": stats["invalid_reasons"],
            "manager_profiles": profiles_written,
        }
    except Exception as exc:
        logger.exception("ingest failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="ingest")
        raise
