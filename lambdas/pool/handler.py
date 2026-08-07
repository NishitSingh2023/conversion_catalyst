"""Pool stage: rank every unassigned lead into a claimable pool.

Any valid lead in the batch that the optimizer did not place lands here so a
manager can pull it manually. Leads reach the pool for one of two reasons, which
is recorded so the dashboard can distinguish them:

    no_eligible_manager - the eligibility filter left the lead with zero
                          candidate managers (wrong language/geography, or no
                          active manager covers it).
    capacity_overflow   - the lead had eligible, scored managers but they were
                          all at the 50-lead cap by the time its turn came.

Ordering is the same business priority the optimizer uses: H before M before L
before EL, and within a tier the highest-scoring lead first (a lead with a
strong potential match is worth a manager's attention sooner). ``best_score`` is
the lead's best conversion probability across managers, or null when it never
reached scoring.
"""
from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text

from shared.constants import INTENT_PRIORITY, POOL_STATUS_AVAILABLE
from shared.db import get_engine, read_sql, write_dataframe
from shared.pipeline import fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_POOL_COLUMNS = [
    "lead_id", "intent_bucket", "priority_rank", "best_score", "reason", "status",
]

# Valid, unassigned leads for the batch, with their best score and whether they
# ever had an eligible manager. The eligibility stage persists *all* eligible
# pairs (only rejections are sampled), so this EXISTS check is exact.
_UNASSIGNED_LEADS = """
SELECT
    n.lead_id,
    n.intent_bucket,
    (SELECT max(s.conversion_probability)
       FROM scores s
      WHERE s.run_id = :run_id AND s.lead_id = n.lead_id) AS best_score,
    EXISTS (SELECT 1 FROM eligibility_matrix e
             WHERE e.run_id = :run_id AND e.lead_id = n.lead_id AND e.eligible) AS had_eligible
FROM new_leads n
WHERE n.batch_id = :batch_id
  AND n.is_valid
  AND NOT EXISTS (SELECT 1 FROM assignments a
                   WHERE a.run_id = :run_id AND a.lead_id = n.lead_id)
"""


def rank_pool(leads: pd.DataFrame, intent_priority: dict[str, int] | None = None) -> pd.DataFrame:
    """Order unassigned leads and assign a 1-based ``priority_rank``.

    Pure function: takes the unassigned-lead frame (``lead_id``,
    ``intent_bucket``, ``best_score``, ``had_eligible``) and returns the rows to
    write to ``pool``.
    """
    if leads.empty:
        return pd.DataFrame(columns=_POOL_COLUMNS)

    intent_priority = intent_priority or INTENT_PRIORITY
    lowest = max(intent_priority.values()) + 1

    out = leads.copy()
    out["_prio"] = out["intent_bucket"].map(lambda b: intent_priority.get(b, lowest))
    # Highest score first within a tier; leads that never scored sort last.
    out = out.sort_values(
        ["_prio", "best_score"], ascending=[True, False], na_position="last"
    ).reset_index(drop=True)

    out["priority_rank"] = out.index + 1
    out["reason"] = out["had_eligible"].map(
        lambda ok: "capacity_overflow" if ok else "no_eligible_manager"
    )
    out["status"] = POOL_STATUS_AVAILABLE
    # NaN best_score -> None so it maps to SQL NULL.
    out["best_score"] = out["best_score"].astype(object).where(out["best_score"].notna(), None)
    return out[_POOL_COLUMNS]


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    batch_id = event.get("batch_id")
    if not run_id:
        raise ValueError("pool requires run_id in the event")

    try:
        if not batch_id:
            raise ValueError("pool requires batch_id in the event")

        leads = read_sql(_UNASSIGNED_LEADS, {"run_id": run_id, "batch_id": batch_id})
        pooled = rank_pool(leads)

        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM pool WHERE run_id = :run_id"), {"run_id": run_id})

        if not pooled.empty:
            pooled = pooled.copy()
            pooled.insert(0, "run_id", run_id)
            write_dataframe(pooled, "pool")

        leads_pooled = int(len(pooled))
        reasons = (
            pooled["reason"].value_counts().to_dict() if not pooled.empty else {}
        )

        update_run(run_id, stage="pool", leads_pooled=leads_pooled)

        logger.info(
            "pool run=%s batch=%s leads_pooled=%s reasons=%s",
            run_id, batch_id, leads_pooled, reasons,
        )

        return {
            **{k: event[k] for k in ("run_id", "batch_id", "business_date", "model_id") if k in event},
            "leads_pooled": leads_pooled,
            "pool_reasons": reasons,
        }
    except Exception as exc:
        logger.exception("pool failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="pool")
        raise
