"""Pipeline run tracking.

Every nightly execution gets a ``run_id`` that threads through all stages and
ties together the rows written to ``eligibility_matrix``, ``scores``,
``assignments`` and ``pool``. The ``pipeline_runs`` table is the audit log the
dashboard reads to show stage progress and failures.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text

from shared.constants import RUN_STATUS_FAILED, RUN_STATUS_RUNNING, RUN_STATUS_SUCCESS
from shared.db import get_engine


def new_run_id() -> str:
    """Human-sortable run id with a short random suffix."""
    return f"run-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def start_run(run_id: str, batch_id: str | None = None, model_id: str | None = None) -> None:
    """Create the pipeline_runs row for a new execution."""
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO pipeline_runs (run_id, batch_id, model_id, status, stage)
                VALUES (:run_id, :batch_id, :model_id, :status, 'ingest')
                ON CONFLICT (run_id) DO NOTHING
                """
            ),
            {
                "run_id": run_id,
                "batch_id": batch_id,
                "model_id": model_id,
                "status": RUN_STATUS_RUNNING,
            },
        )


def update_run(run_id: str, **fields) -> None:
    """Patch mutable columns on a run row (stage, counts, model_id...).

    Only known columns are accepted so callers cannot inject arbitrary SQL.
    """
    allowed = {
        "stage", "status", "model_id", "batch_id",
        "leads_processed", "leads_assigned", "leads_pooled", "errors",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE pipeline_runs SET {assignments} WHERE run_id = :run_id"),
            {**updates, "run_id": run_id},
        )


def complete_run(run_id: str, **fields) -> None:
    """Mark a run successful and stamp completed_at."""
    update_run(run_id, status=RUN_STATUS_SUCCESS, **fields)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET completed_at = now() WHERE run_id = :run_id"),
            {"run_id": run_id},
        )


def fail_run(run_id: str, error: str, stage: str | None = None) -> None:
    """Mark a run failed, recording the error message."""
    update_run(run_id, status=RUN_STATUS_FAILED, errors=str(error)[:4000], stage=stage)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET completed_at = now() WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
