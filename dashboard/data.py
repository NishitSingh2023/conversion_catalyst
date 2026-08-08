"""Read-only data-access layer for the dashboard.

This is the *only* module in the dashboard that talks to Postgres, and it does so
through a single entry point: ``shared.db.read_sql``. Nothing else from
``shared.db`` is imported, so no write path is reachable from the dashboard even
by accident -- the read-only contract is enforced by what is importable here
rather than by review discipline.

Conventions every query in this module follows:

  * One public function per query, taking explicit parameters bound as ``:name``
    placeholders -- never string-interpolated SQL.
  * Only ``SELECT`` statements (or ``SELECT``-only CTEs). No statement that
    mutates rows or schema exists in this module.
  * Run-scoped reads filter on ``WHERE run_id = :run_id`` so every view shows one
    consistent run.
  * Aggregates and counts are computed in SQL, not in pandas, and per-pair tables
    (``scores``, ``eligibility_matrix``) are always read with a ``:limit`` /
    ``:offset`` bound.
  * Agents are identified by ``manager_id`` only; the personal-name column on
    ``manager_profiles`` is never selected, so it cannot be rendered or logged.
  * Configuration is read through ``shared.config`` for the non-secret
    host/port/dbname used in the connection-failure message only. No credential
    is ever read, returned, or logged.

Populated by task 4 (connection probe, caching conventions, and the per-view
queries).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.config import get_config
from shared.constants import INTENT_PRIORITY, MAX_LEADS_PER_MANAGER
from shared.db import read_sql  # the ONLY shared.db import: no write path reachable

__all__ = [
    "INTENT_PRIORITY",
    "MAX_LEADS_PER_MANAGER",
    "count_assignments",
    "count_lead_pairs",
    "count_pool",
    "get_active_model",
    "get_agent_assignment_distribution",
    "get_agent_capacity",
    "get_assignments",
    "get_decision_context",
    "get_default_interesting_leads",
    "get_eligible_agents",
    "get_funnel_reconciliation",
    "get_lead_attributes",
    "get_lead_decision",
    "get_lead_pool_entry",
    "get_lead_scores",
    "get_manager_profiles",
    "get_pool",
    "get_pool_reason_breakdown",
    "get_pool_status_breakdown",
    "get_push_status_breakdown",
    "get_run_header",
    "get_run_lead_ids",
    "get_runs",
    "get_sampled_rejections",
    "has_rejection_rows",
    "probe_connection",
]

# --- Cache TTLs -----------------------------------------------------------
# Slow-changing reference data (model, profiles) is cached for five minutes; the
# run list for thirty seconds so a freshly started run appears without a manual
# refresh; run-scoped reads for fifteen seconds so switching runs feels instant
# while still reflecting an in-flight pipeline. Every cached run-scoped function
# takes run_id as an argument, so run_id is part of the cache key and one run can
# never serve another run's rows.
TTL_REFERENCE = 300
TTL_RUN_LIST = 30
TTL_RUN_SCOPED = 15

# Upper bound on the Explainability lead picker, so populating a selectbox can
# never turn into an unbounded scan of the batch.
LEAD_PICKER_LIMIT = 2_000

# The run's business date, which is the window the capacity cap is enforced over.
# Capacity is deliberately *not* per-run: the optimizer reads `manager_daily_load`,
# which sums an Agent's optimizer assignments plus pool claims across every run
# sharing a business date. Several runs on one date therefore share one pool of 50
# seats per Agent, so a per-run count would understate the load the optimizer
# actually saw and would make the explanation wrong. Reused by the Explainability
# queries as a leading CTE named `bd`.
_RUN_BUSINESS_DATE_CTE = """
        WITH bd AS (
            SELECT max(business_date) AS business_date
            FROM assignments
            WHERE run_id = :run_id
        )"""


# --- Connection probe -----------------------------------------------------

def probe_connection() -> tuple[bool, dict[str, object]]:
    """Check the database is reachable and return non-secret connection details.

    Returns ``(ok, {"host", "port", "dbname"})``. The three values are read as
    named attributes off the resolved config so the password is never touched,
    and the config object itself is never returned, rendered, or logged. The
    exception is deliberately swallowed rather than surfaced: the caller renders a
    one-line message, not a stack trace.
    """
    cfg = get_config().database
    details: dict[str, object] = {
        "host": cfg.host,
        "port": cfg.port,
        "dbname": cfg.dbname,
    }
    try:
        read_sql("SELECT 1 AS ok")
    except Exception:  # noqa: BLE001 - any driver error means "cannot connect"
        return False, details
    return True, details


# --- Run selection / Run History (Req 3, 10) ------------------------------

@st.cache_data(ttl=TTL_RUN_LIST, show_spinner=False)
def get_runs() -> pd.DataFrame:
    """All pipeline runs, most recent first.

    Serves both the sidebar selector and the Run History view. Because the first
    row is the most recent ``started_at``, the app takes ``[0]`` as the default
    Selected_Run and no separate "latest run" query exists. Returns an empty
    DataFrame when no runs exist; the caller renders the Empty_State.
    """
    return read_sql(
        """
        SELECT run_id, started_at, completed_at, status, stage,
               leads_processed, leads_assigned, leads_pooled
        FROM pipeline_runs
        ORDER BY started_at DESC
        """
    )


# --- Pipeline Flow (Req 4) ------------------------------------------------

@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_run_header(run_id: str) -> pd.DataFrame:
    """The selected run's status, stage, timings, counts, and error text."""
    return read_sql(
        """
        SELECT run_id, status, stage, started_at, completed_at,
               leads_processed, leads_assigned, leads_pooled, errors
        FROM pipeline_runs
        WHERE run_id = :run_id
        """,
        {"run_id": run_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_funnel_reconciliation(run_id: str) -> pd.DataFrame:
    """Assigned + pooled = reconciled total, counted in SQL in one round trip."""
    return read_sql(
        """
        SELECT
            (SELECT count(*) FROM assignments WHERE run_id = :run_id) AS assigned,
            (SELECT count(*) FROM pool        WHERE run_id = :run_id) AS pooled,
            (SELECT count(*) FROM assignments WHERE run_id = :run_id)
              + (SELECT count(*) FROM pool    WHERE run_id = :run_id) AS reconciled_total
        """,
        {"run_id": run_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_pool_reason_breakdown(run_id: str) -> pd.DataFrame:
    """Pool row counts per reason (``no_eligible_manager`` / ``capacity_overflow``).

    A reason with no rows is simply absent from the result; the view renders the
    missing reason as 0 rather than hiding it.
    """
    return read_sql(
        """
        SELECT reason, count(*) AS n
        FROM pool
        WHERE run_id = :run_id
        GROUP BY reason
        ORDER BY n DESC
        """,
        {"run_id": run_id},
    )


# --- Model (Req 5) --------------------------------------------------------

@st.cache_data(ttl=TTL_REFERENCE, show_spinner=False)
def get_active_model() -> pd.DataFrame:
    """The single active model row, or an empty frame if none is active.

    ``precision`` is quoted because it collides with the SQL ``DOUBLE PRECISION``
    keyword; unquoted it is a syntax error in this position.
    """
    return read_sql(
        """
        SELECT model_id, trained_at, auc, "precision", recall,
               training_rows, feature_list
        FROM model_registry
        WHERE is_active
        """
    )


# --- Manager Profiles (Req 6) --------------------------------------------

@st.cache_data(ttl=TTL_REFERENCE, show_spinner=False)
def get_manager_profiles() -> pd.DataFrame:
    """One row per Agent, identified by ``manager_id``.

    Two things are deliberate here. ``manager_name`` is absent from the SELECT
    list, so the personal name cannot reach a view even by accident. And the
    intent conversion-rate columns were declared unquoted in the migration, so
    Postgres folded them to lower case; they are aliased back to the mixed-case
    spelling the views display.
    """
    return read_sql(
        """
        SELECT manager_id,
               languages_handled,
               geographies_handled,
               products_handled,
               conv_rate_overall,
               conv_rate_h AS "conv_rate_H",
               conv_rate_m AS "conv_rate_M",
               conv_rate_l AS "conv_rate_L",
               avg_response_mins,
               total_leads_handled,
               last_active_date,
               derived_active_flag
        FROM manager_profiles
        ORDER BY manager_id
        """
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_agent_capacity(run_id: str, cap: int = MAX_LEADS_PER_MANAGER) -> pd.DataFrame:
    """Per-Agent assignment count for this run and remaining headroom vs the cap.

    Aggregated in SQL. ``GREATEST(0, ...)`` keeps remaining capacity from going
    negative if a run ever exceeded the cap. This is the run-scoped load, not the
    business-date load in the ``manager_daily_load`` view, because the dashboard
    is scoped to one run.
    """
    return read_sql(
        """
        SELECT mp.manager_id,
               COALESCE(a.assigned_count, 0)                     AS current_run_assignments,
               GREATEST(0, :cap - COALESCE(a.assigned_count, 0)) AS remaining_capacity
        FROM manager_profiles mp
        LEFT JOIN (
            SELECT primary_manager_id AS manager_id, count(*) AS assigned_count
            FROM assignments
            WHERE run_id = :run_id
            GROUP BY primary_manager_id
        ) a ON a.manager_id = mp.manager_id
        ORDER BY mp.manager_id
        """,
        {"run_id": run_id, "cap": int(cap)},
    )


# --- Assignments (Req 7) -------------------------------------------------

@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_assignments(run_id: str, limit: int, offset: int) -> pd.DataFrame:
    """One page of assignment rows for the run, ordered by ``lead_id``."""
    return read_sql(
        """
        SELECT lead_id, primary_manager_id, fallback_manager_id,
               confidence_score, match_score, intent_bucket, assigned_at, push_status
        FROM assignments
        WHERE run_id = :run_id
        ORDER BY lead_id
        LIMIT :limit OFFSET :offset
        """,
        {"run_id": run_id, "limit": int(limit), "offset": int(offset)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def count_assignments(run_id: str) -> int:
    """Total assignments for the run; drives page navigation."""
    frame = read_sql(
        "SELECT count(*) AS n FROM assignments WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    return int(frame["n"].iloc[0]) if not frame.empty else 0


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_push_status_breakdown(run_id: str) -> pd.DataFrame:
    """Assignment counts per LSQ push status, counted in SQL."""
    return read_sql(
        """
        SELECT push_status, count(*) AS n
        FROM assignments
        WHERE run_id = :run_id
        GROUP BY push_status
        ORDER BY n DESC
        """,
        {"run_id": run_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_agent_assignment_distribution(
    run_id: str, cap: int = MAX_LEADS_PER_MANAGER
) -> pd.DataFrame:
    """Assignments per Agent for the run, flagging Agents sitting exactly at the cap.

    ``at_cap`` is computed in SQL so the view only has to style the rows.
    """
    return read_sql(
        """
        SELECT primary_manager_id AS manager_id,
               count(*)           AS assignment_count,
               (count(*) = :cap)  AS at_cap
        FROM assignments
        WHERE run_id = :run_id
        GROUP BY primary_manager_id
        ORDER BY assignment_count DESC, manager_id
        """,
        {"run_id": run_id, "cap": int(cap)},
    )


# --- Pool (Req 8) --------------------------------------------------------

@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_pool(run_id: str, limit: int, offset: int) -> pd.DataFrame:
    """One page of pool entries for the run, highest priority (lowest rank) first."""
    return read_sql(
        """
        SELECT lead_id, intent_bucket, priority_rank, best_score,
               reason, status, claimed_by, claimed_at
        FROM pool
        WHERE run_id = :run_id
        ORDER BY priority_rank ASC
        LIMIT :limit OFFSET :offset
        """,
        {"run_id": run_id, "limit": int(limit), "offset": int(offset)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def count_pool(run_id: str) -> int:
    """Total pool entries for the run; drives page navigation."""
    frame = read_sql(
        "SELECT count(*) AS n FROM pool WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    return int(frame["n"].iloc[0]) if not frame.empty else 0


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_pool_status_breakdown(run_id: str) -> pd.DataFrame:
    """Pool row counts per status (``available`` / ``claimed``), counted in SQL."""
    return read_sql(
        """
        SELECT status, count(*) AS n
        FROM pool
        WHERE run_id = :run_id
        GROUP BY status
        ORDER BY n DESC
        """,
        {"run_id": run_id},
    )


# --- Explainability (Req 9) ----------------------------------------------

@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_run_lead_ids(run_id: str, limit: int = LEAD_PICKER_LIMIT) -> pd.DataFrame:
    """Bounded list of lead ids that this run decided on, for the lead picker.

    Union of the run's assigned and pooled leads, which is exactly the set of
    leads that have a decision worth explaining. Bounded by ``:limit`` so
    populating the picker is never an unbounded scan.
    """
    return read_sql(
        """
        SELECT lead_id FROM (
            SELECT lead_id FROM assignments WHERE run_id = :run_id
            UNION
            SELECT lead_id FROM pool        WHERE run_id = :run_id
        ) decided
        ORDER BY lead_id
        LIMIT :limit
        """,
        {"run_id": run_id, "limit": int(limit)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def count_lead_pairs(run_id: str, lead_id: str) -> pd.DataFrame:
    """Eligible / rejected / scored pair counts for one lead, in one round trip.

    These drive the page bounds for the three per-pair sections of the
    Explainability view, counted in SQL so the view never has to fetch rows just
    to size them. ``rejected_count`` overlaps with :func:`has_rejection_rows`,
    which stays the gate for the "not sampled" message because presence is the
    question being asked there, not volume.
    """
    return read_sql(
        """
        SELECT
            (SELECT count(*) FROM eligibility_matrix
             WHERE run_id = :run_id AND lead_id = :lead_id AND eligible)     AS eligible_count,
            (SELECT count(*) FROM eligibility_matrix
             WHERE run_id = :run_id AND lead_id = :lead_id AND NOT eligible) AS rejected_count,
            (SELECT count(*) FROM scores
             WHERE run_id = :run_id AND lead_id = :lead_id)                  AS scored_count
        """,
        {"run_id": run_id, "lead_id": lead_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_lead_attributes(lead_id: str) -> pd.DataFrame:
    """The lead's own attributes; empty frame if the id is not in the batch."""
    return read_sql(
        """
        SELECT lead_id, intent_bucket, geography, language, product_interest
        FROM new_leads
        WHERE lead_id = :lead_id
        """,
        {"lead_id": lead_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_eligible_agents(
    run_id: str, lead_id: str, limit: int, offset: int
) -> pd.DataFrame:
    """Agents that passed eligibility for this lead on this run, paged."""
    return read_sql(
        """
        SELECT manager_id
        FROM eligibility_matrix
        WHERE run_id = :run_id AND lead_id = :lead_id AND eligible
        ORDER BY manager_id
        LIMIT :limit OFFSET :offset
        """,
        {"run_id": run_id, "lead_id": lead_id, "limit": int(limit), "offset": int(offset)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_lead_scores(
    run_id: str,
    lead_id: str,
    limit: int,
    offset: int,
    cap: int = MAX_LEADS_PER_MANAGER,
) -> pd.DataFrame:
    """This lead's candidate Agents, best score first, with their run load.

    The score alone does not explain the outcome. The optimizer walks this exact
    ranking and takes the first Agent with a free seat, so the load column is what
    turns "these were the scores" into "and this is why the top one did not get
    it". ``rank`` is computed with a window function over the lead's whole
    candidate set, so it stays globally correct across pages rather than
    restarting at 1 on page two.

    Scoped by both ``run_id`` and ``lead_id`` and always bounded, so the
    leads x managers cross-join is never materialized.
    """
    return read_sql(
        f"""
        {_RUN_BUSINESS_DATE_CTE},
        run_loads AS (
            SELECT primary_manager_id AS manager_id, count(*) AS run_load
            FROM assignments
            WHERE run_id = :run_id
            GROUP BY primary_manager_id
        )
        SELECT s.manager_id,
               s.conversion_probability,
               row_number() OVER (
                   ORDER BY s.conversion_probability DESC, s.manager_id
               )                                   AS rank,
               COALESCE(rl.run_load, 0)            AS run_load,
               COALESCE(dl.load, 0)                AS date_load,
               (COALESCE(dl.load, 0) >= :cap)      AS at_cap
        FROM scores s
        LEFT JOIN run_loads rl ON rl.manager_id = s.manager_id
        LEFT JOIN manager_daily_load dl
               ON dl.manager_id = s.manager_id
              AND dl.business_date = (SELECT business_date FROM bd)
        WHERE s.run_id = :run_id AND s.lead_id = :lead_id
        ORDER BY s.conversion_probability DESC, s.manager_id
        LIMIT :limit OFFSET :offset
        """,
        {
            "run_id": run_id,
            "lead_id": lead_id,
            "cap": int(cap),
            "limit": int(limit),
            "offset": int(offset),
        },
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_lead_decision(run_id: str, lead_id: str) -> pd.DataFrame:
    """The assignment this lead received, if it was assigned at all.

    Carries the business-date load of both the chosen and the fallback Agent,
    counted in SQL on the same basis the optimizer enforced the cap, so the view
    can say whether the recorded fallback still had a seat by the end of the date.
    """
    return read_sql(
        """
        SELECT a.lead_id,
               a.primary_manager_id,
               a.fallback_manager_id,
               a.confidence_score,
               a.match_score,
               a.intent_bucket,
               a.assigned_at,
               a.business_date,
               a.push_status,
               COALESCE(pl.load, 0) AS primary_date_load,
               COALESCE(fl.load, 0) AS fallback_date_load
        FROM assignments a
        LEFT JOIN manager_daily_load pl
               ON pl.manager_id = a.primary_manager_id
              AND pl.business_date = a.business_date
        LEFT JOIN manager_daily_load fl
               ON fl.manager_id = a.fallback_manager_id
              AND fl.business_date = a.business_date
        WHERE a.run_id = :run_id AND a.lead_id = :lead_id
        """,
        {"run_id": run_id, "lead_id": lead_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_lead_pool_entry(run_id: str, lead_id: str) -> pd.DataFrame:
    """The pool entry this lead received, if it was pooled instead of assigned."""
    return read_sql(
        """
        SELECT lead_id, intent_bucket, priority_rank, best_score,
               reason, status, claimed_by, claimed_at
        FROM pool
        WHERE run_id = :run_id AND lead_id = :lead_id
        """,
        {"run_id": run_id, "lead_id": lead_id},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_decision_context(
    run_id: str, lead_id: str, cap: int = MAX_LEADS_PER_MANAGER
) -> pd.DataFrame:
    """Where the chosen Agent sat in the ranking, and how many above were full.

    This is the query that makes the capacity fall-through legible: if the lead
    went to the third-best Agent, ``higher_ranked_at_cap`` says how many
    better-scoring Agents finished the run at the cap. Computed entirely in SQL and
    independent of paging, so the explanation holds no matter which page of
    candidates is on screen.

    ``primary_rank`` is null when the lead was not assigned.
    """
    return read_sql(
        f"""
        {_RUN_BUSINESS_DATE_CTE},
        ranked AS (
            SELECT s.manager_id,
                   row_number() OVER (
                       ORDER BY s.conversion_probability DESC, s.manager_id
                   ) AS rank
            FROM scores s
            WHERE s.run_id = :run_id AND s.lead_id = :lead_id
        ),
        chosen AS (
            SELECT r.rank
            FROM ranked r
            JOIN assignments a
              ON a.run_id = :run_id
             AND a.lead_id = :lead_id
             AND a.primary_manager_id = r.manager_id
        )
        SELECT
            (SELECT count(*) FROM ranked)  AS candidate_count,
            (SELECT rank FROM chosen)      AS primary_rank,
            (SELECT count(*)
               FROM ranked r
               LEFT JOIN manager_daily_load dl
                      ON dl.manager_id = r.manager_id
                     AND dl.business_date = (SELECT business_date FROM bd)
              WHERE r.rank < (SELECT rank FROM chosen)
                AND COALESCE(dl.load, 0) >= :cap) AS higher_ranked_at_cap
        """,
        {"run_id": run_id, "lead_id": lead_id, "cap": int(cap)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_sampled_rejections(
    run_id: str, lead_id: str, limit: int, offset: int
) -> pd.DataFrame:
    """Rejected Agents and reasons for this lead, where sampling captured them."""
    return read_sql(
        """
        SELECT manager_id, rejection_reason
        FROM eligibility_matrix
        WHERE run_id = :run_id AND lead_id = :lead_id AND NOT eligible
        ORDER BY manager_id
        LIMIT :limit OFFSET :offset
        """,
        {"run_id": run_id, "lead_id": lead_id, "limit": int(limit), "offset": int(offset)},
    )


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def has_rejection_rows(run_id: str, lead_id: str) -> bool:
    """Whether rejection detail was persisted for this lead at all.

    The eligibility stage samples which leads get rejection rows, so ``False``
    means "not sampled", never "no Agent was rejected". The view must say so.
    """
    frame = read_sql(
        """
        SELECT EXISTS (
            SELECT 1 FROM eligibility_matrix
            WHERE run_id = :run_id AND lead_id = :lead_id AND NOT eligible
        ) AS has_rejections
        """,
        {"run_id": run_id, "lead_id": lead_id},
    )
    return bool(frame["has_rejections"].iloc[0]) if not frame.empty else False


@st.cache_data(ttl=TTL_RUN_SCOPED, show_spinner=False)
def get_default_interesting_leads(run_id: str) -> pd.DataFrame:
    """Three leads worth opening the view on, fetched in one round trip.

    Each shows a different branch of the engine's behaviour: the
    highest-confidence assignment (decisive first-choice placement), the
    deepest fall-through (better-scoring Agents were full, so a lower-ranked one
    took it), and the top ``capacity_overflow`` pool lead (every candidate was
    full). Any of them can be null on a run that produced no such case.
    """
    return read_sql(
        """
        WITH ranked AS (
            SELECT s.lead_id, s.manager_id,
                   row_number() OVER (
                       PARTITION BY s.lead_id
                       ORDER BY s.conversion_probability DESC, s.manager_id
                   ) AS rank
            FROM scores s
            WHERE s.run_id = :run_id
        )
        SELECT
            (SELECT lead_id FROM assignments
             WHERE run_id = :run_id
             ORDER BY confidence_score DESC NULLS LAST
             LIMIT 1) AS top_confidence_lead_id,
            (SELECT a.lead_id
             FROM assignments a
             JOIN ranked r ON r.lead_id = a.lead_id
                          AND r.manager_id = a.primary_manager_id
             WHERE a.run_id = :run_id AND r.rank > 1
             ORDER BY r.rank DESC, a.lead_id
             LIMIT 1) AS fallthrough_lead_id,
            (SELECT lead_id FROM pool
             WHERE run_id = :run_id AND reason = 'capacity_overflow'
             ORDER BY priority_rank ASC
             LIMIT 1) AS overflow_lead_id
        """,
        {"run_id": run_id},
    )
