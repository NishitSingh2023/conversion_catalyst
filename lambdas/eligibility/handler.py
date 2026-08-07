"""Eligibility stage: narrow the candidate managers for each lead.

Before any ML scoring happens, managers who simply cannot take a lead are
removed. This keeps the scoring matrix small and makes the final assignment
explainable ("these 6 reps were eligible, the rest were filtered because...").

Filter rules, all derived from ``manager_profiles`` (there is no managers
table - every attribute comes from ``lead_manager_history``):

    inactive        - no interaction inside the activity window
    at_capacity     - already holding MAX_LEADS_PER_MANAGER leads today
    language_mismatch  - lead's language not in the rep's handled languages
    geography_mismatch - lead's geography not in the rep's handled geographies

Availability is approximated by recent activity because the hackathon dataset
carries no live availability/roster feed. That assumption is recorded in the
rejection reason so it is visible in the dashboard rather than hidden.

Persistence: every *eligible* pair is written to ``eligibility_matrix``.
Rejections are written for a bounded sample of leads (``REJECTION_SAMPLE_LEADS``)
because a full rejection matrix is |leads| x |managers| rows - 3M+ at 600 reps -
which would dwarf the useful data without adding insight.

Leads left with no eligible manager are returned as ``unassignable`` so the pool
stage can park them with a reason instead of dropping them.
"""
from __future__ import annotations

import logging
import os

import pandas as pd
from sqlalchemy import text

from shared.constants import MAX_LEADS_PER_MANAGER
from shared.db import get_engine, read_sql, write_dataframe
from shared.pipeline import fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# How many leads' rejections to persist for explainability.
REJECTION_SAMPLE_LEADS = int(os.getenv("REJECTION_SAMPLE_LEADS", "200"))


def current_loads() -> dict[str, int]:
    """Leads already assigned to each manager today (across runs).

    Counted so that re-runs and same-day incremental runs respect the cap rather
    than handing a rep another 50 leads.
    """
    df = read_sql(
        """
        SELECT primary_manager_id AS manager_id, count(*) AS load
        FROM assignments
        WHERE assigned_at::date = current_date
        GROUP BY primary_manager_id
        """
    )
    if df.empty:
        return {}
    return dict(zip(df["manager_id"], df["load"].astype(int), strict=False))


def evaluate_pairs(
    leads: pd.DataFrame,
    profiles: pd.DataFrame,
    loads: dict[str, int] | None = None,
    max_leads: int = MAX_LEADS_PER_MANAGER,
) -> pd.DataFrame:
    """Cross leads with managers and label each pair eligible or rejected.

    Returns a frame with ``lead_id, manager_id, eligible, rejection_reason``.
    Pure function - no DB access - so the rules are unit-testable.
    """
    if leads.empty or profiles.empty:
        return pd.DataFrame(columns=["lead_id", "manager_id", "eligible", "rejection_reason"])

    loads = loads or {}
    rows = []
    profile_records = profiles.to_dict(orient="records")

    for lead in leads.to_dict(orient="records"):
        for prof in profile_records:
            manager_id = prof["manager_id"]
            reason = None

            if not prof.get("derived_active_flag"):
                reason = "inactive_no_recent_activity"
            elif loads.get(manager_id, 0) >= max_leads:
                reason = "at_capacity"
            elif lead.get("language") not in (prof.get("languages_handled") or []):
                reason = "language_mismatch"
            elif lead.get("geography") not in (prof.get("geographies_handled") or []):
                reason = "geography_mismatch"

            rows.append(
                {
                    "lead_id": lead["lead_id"],
                    "manager_id": manager_id,
                    "eligible": reason is None,
                    "rejection_reason": reason,
                }
            )

    return pd.DataFrame(rows)


def _persist(run_id: str, pairs: pd.DataFrame) -> tuple[int, int]:
    """Write eligible pairs plus a bounded sample of rejections."""
    eligible = pairs[pairs["eligible"]].copy()
    rejected = pairs[~pairs["eligible"]].copy()

    sample_leads = sorted(pairs["lead_id"].unique())[:REJECTION_SAMPLE_LEADS]
    rejected_sample = rejected[rejected["lead_id"].isin(sample_leads)]

    out = pd.concat([eligible, rejected_sample], ignore_index=True)
    out["run_id"] = run_id

    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM eligibility_matrix WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
    write_dataframe(
        out[["run_id", "lead_id", "manager_id", "eligible", "rejection_reason"]],
        "eligibility_matrix",
    )
    return len(eligible), len(rejected_sample)


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event["run_id"]
    batch_id = event.get("batch_id")

    try:
        leads = read_sql(
            "SELECT * FROM new_leads WHERE batch_id = :batch_id",
            {"batch_id": batch_id},
        )
        profiles = read_sql("SELECT * FROM manager_profiles")

        pairs = evaluate_pairs(leads, profiles, loads=current_loads())
        n_eligible, n_rejected_sample = _persist(run_id, pairs)

        eligible = pairs[pairs["eligible"]]
        leads_with_options = set(eligible["lead_id"].unique())
        unassignable = sorted(set(leads["lead_id"]) - leads_with_options)

        update_run(run_id, stage="eligibility")

        logger.info(
            "eligibility run=%s leads=%s managers=%s eligible_pairs=%s unassignable=%s",
            run_id, len(leads), len(profiles), n_eligible, len(unassignable),
        )

        return {
            **event,
            "eligible_pairs": n_eligible,
            "rejected_pairs_sampled": n_rejected_sample,
            "leads_with_candidates": len(leads_with_options),
            "unassignable_leads": unassignable,
            "managers_considered": int(len(profiles)),
        }
    except Exception as exc:
        logger.exception("eligibility failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="eligibility")
        raise
