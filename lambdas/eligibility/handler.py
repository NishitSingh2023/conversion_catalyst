"""Eligibility stage: narrow the candidate managers for each lead.

Before any ML scoring, managers who simply cannot take a lead are removed. This
keeps the scoring matrix small and makes the final assignment explainable
("these 6 reps were eligible, the rest were filtered because...").

Filter rules, all derived from ``manager_profiles`` - there is no managers table,
every attribute comes from ``lead_manager_history``:

    inactive_no_recent_activity - no interaction inside the activity window
    at_capacity                 - already holding MAX_LEADS_PER_MANAGER today
    language_mismatch           - lead's language not among handled languages
    geography_mismatch          - lead's geography not among handled geographies

Availability is approximated by recent activity because the dataset carries no
live roster feed. The assumption is recorded in the rejection reason so it is
visible in the dashboard rather than hidden.

Why this runs as SQL rather than pandas
---------------------------------------
The candidate set is |leads| x |managers|: 3,000,000 rows at the target scale of
600 managers and 5,000 leads. Materialising that in Python cost ~1GB of resident
memory, more than the function's limit, to then discard 99% of it. Postgres does
the cross join, applies the predicates and writes the surviving rows via
INSERT ... SELECT, so only aggregate counts cross the network. Capacity comes
from the ``manager_daily_load`` view, the single definition shared with the
optimizer and the pool claim path.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text

from shared.constants import MAX_LEADS_PER_MANAGER
from shared.db import get_engine, read_sql
from shared.pipeline import fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# How many leads' rejections to persist. A full rejection matrix is
# |leads| x |managers| rows - 3M+ at 600 reps - which would swamp the useful
# data without adding insight, so explainability is sampled.
REJECTION_SAMPLE_LEADS = int(os.getenv("REJECTION_SAMPLE_LEADS", "200"))

# Candidate pairs with the four rule outcomes evaluated per pair. Reused by the
# eligible insert, the rejection insert and the capacity summary so the rules are
# written exactly once.
_CANDIDATES_CTE = """
WITH loads AS (
    SELECT manager_id, load
    FROM manager_daily_load
    WHERE business_date = :business_date
),
candidates AS (
    SELECT
        l.lead_id,
        m.manager_id,
        m.derived_active_flag                       AS active_ok,
        COALESCE(ld.load, 0) < :max_leads           AS capacity_ok,
        l.language  = ANY(m.languages_handled)      AS language_ok,
        l.geography = ANY(m.geographies_handled)    AS geography_ok
    FROM new_leads l
    CROSS JOIN manager_profiles m
    LEFT JOIN loads ld ON ld.manager_id = m.manager_id
    WHERE l.batch_id = :batch_id
      AND l.is_valid
)
"""

# Reason precedence is deliberate: report the most fundamental blocker first, so
# an inactive rep is not reported as merely "language mismatch".
_REASON_CASE = """
    CASE
        WHEN NOT active_ok   THEN 'inactive_no_recent_activity'
        WHEN NOT capacity_ok THEN 'at_capacity'
        WHEN NOT language_ok THEN 'language_mismatch'
        WHEN NOT geography_ok THEN 'geography_mismatch'
    END
"""

_IS_ELIGIBLE = "active_ok AND capacity_ok AND language_ok AND geography_ok"

_INSERT_ELIGIBLE = f"""
{_CANDIDATES_CTE}
INSERT INTO eligibility_matrix (run_id, lead_id, manager_id, eligible, rejection_reason)
SELECT :run_id, lead_id, manager_id, TRUE, NULL
FROM candidates
WHERE {_IS_ELIGIBLE}
"""

_INSERT_REJECTED_SAMPLE = f"""
{_CANDIDATES_CTE},
sample_leads AS (
    SELECT lead_id FROM new_leads
    WHERE batch_id = :batch_id AND is_valid
    ORDER BY lead_id
    LIMIT :sample_size
)
INSERT INTO eligibility_matrix (run_id, lead_id, manager_id, eligible, rejection_reason)
SELECT :run_id, c.lead_id, c.manager_id, FALSE, {_REASON_CASE}
FROM candidates c
JOIN sample_leads s USING (lead_id)
WHERE NOT ({_IS_ELIGIBLE})
"""


def _business_date(event: dict) -> str:
    """Business date owning this run's capacity window.

    Passed explicitly rather than derived from ``current_date`` so a retry that
    crosses midnight UTC keeps counting against the same day. Defaults to the
    RDS current date only when absent.
    """
    if event.get("business_date"):
        return str(event["business_date"])
    return str(read_sql("SELECT current_date AS d").iloc[0]["d"])


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    batch_id = event.get("batch_id")

    # Guard inputs before the try so a malformed event cannot leave a run row
    # stuck in 'running' - but only once run_id is known can we record failure.
    if not run_id:
        raise ValueError("eligibility requires run_id in the event")

    try:
        if not batch_id:
            # Without this an empty batch_id would match no rows and report
            # success with zero eligible pairs.
            raise ValueError("eligibility requires batch_id in the event")

        business_date = _business_date(event)
        params = {
            "run_id": run_id,
            "batch_id": batch_id,
            "business_date": business_date,
            "max_leads": MAX_LEADS_PER_MANAGER,
        }

        with get_engine().begin() as conn:
            # Idempotent re-run: clear this run's prior rows first.
            conn.execute(
                text("DELETE FROM eligibility_matrix WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            eligible_rows = conn.execute(text(_INSERT_ELIGIBLE), params).rowcount
            rejected_rows = conn.execute(
                text(_INSERT_REJECTED_SAMPLE),
                {**params, "sample_size": REJECTION_SAMPLE_LEADS},
            ).rowcount

        summary = read_sql(
            """
            SELECT
                (SELECT count(*) FROM new_leads
                  WHERE batch_id = :batch_id AND is_valid)              AS leads_valid,
                (SELECT count(DISTINCT lead_id) FROM eligibility_matrix
                  WHERE run_id = :run_id AND eligible)                  AS leads_with_candidates,
                (SELECT count(*) FROM manager_profiles)                 AS managers_total,
                (SELECT count(*) FROM manager_profiles
                  WHERE derived_active_flag)                            AS managers_active
            """,
            {"batch_id": batch_id, "run_id": run_id},
        ).iloc[0]

        leads_valid = int(summary["leads_valid"])
        leads_with_candidates = int(summary["leads_with_candidates"])
        unassignable = leads_valid - leads_with_candidates

        update_run(run_id, stage="eligibility")

        logger.info(
            "eligibility run=%s batch=%s date=%s eligible_pairs=%s "
            "leads_with_candidates=%s unassignable=%s active_managers=%s/%s",
            run_id, batch_id, business_date, eligible_rows, leads_with_candidates,
            unassignable, summary["managers_active"], summary["managers_total"],
        )

        # Counts only. Lead-id lists are deliberately not returned: Step
        # Functions caps state at 256KB and every stage forwards the previous
        # payload, so an unbounded list crosses the limit mid-run. Downstream
        # stages read their real inputs from Postgres by run_id.
        return {
            "run_id": run_id,
            "batch_id": batch_id,
            "business_date": business_date,
            "eligible_pairs": int(eligible_rows),
            "rejected_pairs_sampled": int(rejected_rows),
            "leads_valid": leads_valid,
            "leads_with_candidates": leads_with_candidates,
            "unassignable_leads": unassignable,
            "managers_active": int(summary["managers_active"]),
            "managers_total": int(summary["managers_total"]),
        }
    except Exception as exc:
        logger.exception("eligibility failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="eligibility")
        raise
