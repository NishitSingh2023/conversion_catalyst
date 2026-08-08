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
The candidate set is |leads| x |managers|: 28,600,000 rows on the real dataset
(30,000 leads x 953 managers). Materialising that in Python cost ~1GB of
resident memory, more than the function's limit, to then discard most of it.
Postgres does the cross join, applies the predicates and writes the surviving
rows via INSERT ... SELECT, so only aggregate counts cross the network. Capacity
comes from the ``manager_daily_load`` view, the single definition shared with the
optimizer and the pool claim path.

Why the surviving set is also capped
------------------------------------
Passing the filters is not selective enough on the real data: 11,618,310 of the
28.6M pairs survive, about 433 managers per lead, because a region+language
cohort is genuinely large. Writing all of them would put ~11.6M rows in
``eligibility_matrix`` and another ~11.6M in ``scores`` on every run - multiple GB
per nightly run against a 20GB disk - and the optimizer loads the whole scored
set into a 4096MB function. So eligibility shortlists at most
``ELIGIBLE_MANAGERS_PER_LEAD`` candidates per lead, in SQL.

A shortlist is a retrieval step, not a decision, and it has one hard obligation:
between them the shortlists must still reach the whole roster, or they silently
become the capacity constraint. ``_SHORTLIST_CTE`` documents how that is achieved
and what it measured when it was not.
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
# |leads| x |managers| rows - 28.6M on the real dataset - which would swamp the
# useful data without adding insight, so explainability is sampled.
REJECTION_SAMPLE_LEADS = int(os.getenv("REJECTION_SAMPLE_LEADS", "200"))

# How many eligible managers to shortlist per lead. Sized against capacity: each
# manager can hold at most MAX_LEADS_PER_MANAGER (50) leads, so 902 active
# managers offer 45,100 seats for ~27k assignable leads. A 50-deep shortlist
# leaves the optimizer far more capacity than it needs to route around managers
# that fill up as it works down the priority order - provided the shortlists
# between them reach the whole roster, which is what ELIGIBLE_TOP_BY_CONV_RATE
# below is about.
ELIGIBLE_MANAGERS_PER_LEAD = int(os.getenv("ELIGIBLE_MANAGERS_PER_LEAD", "50"))

# How many of those slots are reserved for the highest-converting managers in the
# lead's candidate cohort; the rest are sampled across the cohort. See
# ``_SHORTLIST_CTE`` for why the split exists and what happens without it. Set
# this equal to ELIGIBLE_MANAGERS_PER_LEAD to rank purely by conversion rate.
# Clamped where it is used, not here, so the two settings cannot be configured
# into a contradiction.
ELIGIBLE_TOP_BY_CONV_RATE = int(os.getenv("ELIGIBLE_TOP_BY_CONV_RATE", "10"))

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
        l.geography = ANY(m.geographies_handled)    AS geography_ok,
        m.conv_rate_overall                         AS conv_rate_overall
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

# Pick each lead's shortlist from the managers that passed the rules. Four things
# about this:
#
# * **It happens in SQL, not Python.** The whole point of the cap is that the
#   11.6M-row eligible set never materialises anywhere - not in
#   eligibility_matrix, not in the optimizer's frame, and not in this function's
#   memory. Window functions inside the INSERT ... SELECT mean Postgres ranks and
#   discards in one pass and only the surviving ~1.3M rows are ever written.
#
# * **This is candidate generation, not the assignment decision.** It is a
#   recommender-style retrieval step: cheaply produce a small, plausible
#   shortlist, then let the model score it and the optimizer choose. The per-pair
#   ML score (which does depend on the lead) and the capacity-aware greedy match
#   both still run over the shortlist, so who actually gets a lead is decided
#   exactly as before - only the depth of the candidate list is bounded.
#
# * **The shortlist has to stay diverse across leads, and ranking purely by
#   manager quality does not.** ``conv_rate_overall`` is a property of the
#   manager, not of the pair, so within one candidate cohort it orders every
#   lead's list identically. The cohorts are not fine-grained enough to break
#   that up: the 27k valid leads fall into only 50 (language, geography)
#   combinations, and 99% of history is Hindi. Ranking on conv_rate alone was
#   measured end to end and put just 196 of 902 active managers on any shortlist
#   at all, which caps real capacity at 196 x 50 = 9,800 seats. 9,070 leads were
#   assigned and 17,736 overflowed to the pool for want of a candidate with a free
#   seat, while 700 managers sat idle. The shortlist must not decide capacity.
#
# * **So the shortlist is stratified: quality first, then spread.**
#     - ``ELIGIBLE_TOP_BY_CONV_RATE`` slots go to the cohort's highest-converting
#       managers, so every lead is guaranteed to see the best of its cohort.
#     - The remaining slots are a uniform sample of the rest of the cohort, keyed
#       on a hash of the pair, so different leads draw different managers and the
#       optimizer always has somewhere to route once the stars fill up.
#   Sampling costs little quality: with ~433 eligible managers per lead, a 40-slot
#   sample contains one of the cohort's top decile with probability 1 - 0.9^40,
#   i.e. ~98.5%, and the guaranteed quality slots cover the rest.
#
#   The hash is over the business keys, so it is stable across re-runs and across
#   a data reload - unlike anything derived from row ids or heap order. Ties in
#   the quality ranking break on manager_id for the same reason.
_SHORTLIST_CTE = f"""
ranked AS (
    SELECT
        lead_id,
        manager_id,
        ROW_NUMBER() OVER (
            PARTITION BY lead_id
            ORDER BY conv_rate_overall DESC NULLS LAST, manager_id
        ) AS quality_rank,
        ROW_NUMBER() OVER (
            PARTITION BY lead_id
            ORDER BY md5(lead_id || '|' || manager_id)
        ) AS spread_rank
    FROM candidates
    WHERE {_IS_ELIGIBLE}
),
shortlist AS (
    -- A union of the two strata, so a manager that qualifies on both is kept
    -- once. That makes :shortlist_size an upper bound rather than an exact
    -- count, which is the intent - it is a ceiling on what gets written.
    SELECT lead_id, manager_id
    FROM ranked
    WHERE quality_rank <= :quality_slots
       OR spread_rank <= :shortlist_size - :quality_slots
)
"""

_INSERT_ELIGIBLE = f"""
{_CANDIDATES_CTE},
{_SHORTLIST_CTE}
INSERT INTO eligibility_matrix (run_id, lead_id, manager_id, eligible, rejection_reason)
SELECT :run_id, lead_id, manager_id, TRUE, NULL
FROM shortlist
"""

# Explainability rows, for a bounded sample of leads. Deliberately NOT capped the
# same way as the eligible insert: the point of this table is to answer "why was
# this rep not offered my lead?", which needs every rep accounted for, so for a
# sampled lead it records one row per manager that did not make the shortlist.
#
# Two ways a manager can miss out, and they mean different things:
#   * failed a rule           - reported with the specific rule (_REASON_CASE)
#   * passed every rule but
#     ranked below the cap    - reported as 'not_shortlisted'
# Without the second case a sampled lead's rows would no longer partition the
# manager set (50 eligible + ~520 rejected out of 953), leaving the rest silently
# missing from the dashboard's rejection view.
_INSERT_REJECTED_SAMPLE = f"""
{_CANDIDATES_CTE},
{_SHORTLIST_CTE},
sample_leads AS (
    SELECT lead_id FROM new_leads
    WHERE batch_id = :batch_id AND is_valid
    ORDER BY lead_id
    LIMIT :sample_size
)
INSERT INTO eligibility_matrix (run_id, lead_id, manager_id, eligible, rejection_reason)
SELECT :run_id, c.lead_id, c.manager_id, FALSE,
       CASE WHEN {_IS_ELIGIBLE} THEN 'not_shortlisted' ELSE {_REASON_CASE} END
FROM candidates c
JOIN sample_leads s USING (lead_id)
LEFT JOIN shortlist sl
       ON sl.lead_id = c.lead_id
      AND sl.manager_id = c.manager_id
WHERE sl.lead_id IS NULL
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
        # More quality slots than the shortlist holds would make the spread
        # stratum's bound negative and silently disable it, so the quality slice
        # is clamped to the shortlist size here rather than trusted.
        quality_slots = min(ELIGIBLE_TOP_BY_CONV_RATE, ELIGIBLE_MANAGERS_PER_LEAD)
        params = {
            "run_id": run_id,
            "batch_id": batch_id,
            "business_date": business_date,
            "max_leads": MAX_LEADS_PER_MANAGER,
            "shortlist_size": ELIGIBLE_MANAGERS_PER_LEAD,
            "quality_slots": quality_slots,
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
                (SELECT count(DISTINCT manager_id) FROM eligibility_matrix
                  WHERE run_id = :run_id AND eligible)                  AS managers_shortlisted,
                (SELECT count(*) FROM manager_profiles)                 AS managers_total,
                (SELECT count(*) FROM manager_profiles
                  WHERE derived_active_flag)                            AS managers_active,
                -- Active managers that still had a free seat, i.e. the ones the
                -- rules let through on capacity grounds. This is the fair
                -- denominator for judging shortlist breadth: a manager already
                -- holding 50 leads is excluded by the cap, not by the shortlist.
                (SELECT count(*) FROM manager_profiles m
                  LEFT JOIN manager_daily_load l
                         ON l.manager_id = m.manager_id
                        AND l.business_date = :business_date
                  WHERE m.derived_active_flag
                    AND COALESCE(l.load, 0) < :max_leads)               AS managers_with_capacity
            """,
            {
                "batch_id": batch_id,
                "run_id": run_id,
                "business_date": business_date,
                "max_leads": MAX_LEADS_PER_MANAGER,
            },
        ).iloc[0]

        leads_valid = int(summary["leads_valid"])
        leads_with_candidates = int(summary["leads_with_candidates"])
        managers_shortlisted = int(summary["managers_shortlisted"])
        managers_active = int(summary["managers_active"])
        managers_with_capacity = int(summary["managers_with_capacity"])
        unassignable = leads_valid - leads_with_candidates

        update_run(run_id, stage="eligibility")

        # Reachable seats: how much capacity the shortlists actually expose to the
        # optimizer. Capping the shortlist is safe only while the shortlists
        # between them still reach the whole available roster; if they do not, the
        # rest of the team's capacity is invisible however free it is, and leads
        # overflow to the pool while managers sit idle.
        #
        # Compared against managers that *had* a free seat, not against every
        # active manager. On a second run for the same business date most of the
        # team is legitimately full, and that is the roster being the constraint,
        # which is fine - it is the shortlist being the constraint that is not.
        reachable_seats = managers_shortlisted * MAX_LEADS_PER_MANAGER
        if (
            managers_shortlisted < managers_with_capacity
            and reachable_seats < leads_with_candidates
        ):
            logger.warning(
                "eligibility run=%s shortlists reach only %s of the %s managers with "
                "a free seat (%s seats for %s leads with candidates) - the shortlist "
                "is capping capacity, not the roster",
                run_id, managers_shortlisted, managers_with_capacity,
                reachable_seats, leads_with_candidates,
            )

        logger.info(
            "eligibility run=%s batch=%s date=%s eligible_pairs=%s "
            "(cap=%s/lead, %s by conv_rate) leads_with_candidates=%s unassignable=%s "
            "managers_shortlisted=%s with_capacity=%s active=%s/%s",
            run_id, batch_id, business_date, eligible_rows,
            ELIGIBLE_MANAGERS_PER_LEAD, quality_slots,
            leads_with_candidates, unassignable, managers_shortlisted,
            managers_with_capacity, managers_active, summary["managers_total"],
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
            "eligible_managers_per_lead": ELIGIBLE_MANAGERS_PER_LEAD,
            "rejected_pairs_sampled": int(rejected_rows),
            "leads_valid": leads_valid,
            "leads_with_candidates": leads_with_candidates,
            "unassignable_leads": unassignable,
            "managers_shortlisted": managers_shortlisted,
            "managers_with_capacity": managers_with_capacity,
            "managers_active": managers_active,
            "managers_total": int(summary["managers_total"]),
        }
    except Exception as exc:
        logger.exception("eligibility failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="eligibility")
        raise
