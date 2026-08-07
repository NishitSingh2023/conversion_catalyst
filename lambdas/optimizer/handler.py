"""Optimizer stage: turn per-pair scores into one assignment per lead.

Reads the ``scores`` table (one conversion probability per eligible
lead-manager pair), assigns each lead to the manager most likely to convert it,
and writes one row per assigned lead to ``assignments`` with a fallback manager
and a confidence margin. Leads that cannot be placed (every scored manager is
already full) are simply left unassigned here; the pool stage sweeps them up.

Assignment strategy
-------------------
This is a *capacitated* assignment: each manager may hold at most
``MAX_LEADS_PER_MANAGER`` leads on the business date, and managers already carry
load from earlier in the day (the ``manager_daily_load`` view). A textbook
Hungarian match assumes one-to-one and no pre-existing load, so it does not fit
directly; a min-cost-flow / LP would find the global score-maximising placement
but is harder to explain and to justify per-lead in the dashboard.

We use greedy assignment ordered by business priority, which the plan lists as
an accepted strategy and which gives two properties the demo needs:

  * **H leads get first pick.** Leads are processed in intent-priority order
    (H, M, L, EL) and, within a tier, strongest-match first, so high-intent
    leads claim the best-matching managers before lower tiers compete for them.
  * **Explainable fallbacks.** Each lead records the next-best manager it would
    route to if the primary declines, and a confidence = primary - fallback
    margin, both of which fall straight out of the per-lead ranking.

The core ``optimize_assignments`` function is pure (no DB) so the strategy is
unit-testable on small frames; ``lambda_handler`` wires it to Postgres.
"""
from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import text

from shared.constants import INTENT_PRIORITY, MAX_LEADS_PER_MANAGER
from shared.db import get_engine, read_sql, write_dataframe
from shared.pipeline import fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ASSIGNMENT_COLUMNS = [
    "lead_id", "primary_manager_id", "fallback_manager_id",
    "confidence_score", "match_score", "intent_bucket",
]

# Scored pairs joined to the lead's intent bucket. Only valid leads reach here.
_SCORED_PAIRS = """
SELECT s.lead_id, s.manager_id, s.conversion_probability, n.intent_bucket
FROM scores s
JOIN new_leads n ON n.lead_id = s.lead_id
WHERE s.run_id = :run_id AND n.is_valid
"""

# Load already held per manager on this business date (auto-assigned + claimed),
# from the single shared capacity view.
_CURRENT_LOAD = """
SELECT manager_id, load
FROM manager_daily_load
WHERE business_date = :business_date
"""


def _empty_assignments() -> pd.DataFrame:
    return pd.DataFrame(columns=_ASSIGNMENT_COLUMNS)


def optimize_assignments(
    scores: pd.DataFrame,
    remaining_capacity: dict[str, int],
    intent_priority: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Greedily assign each lead to its best manager within capacity.

    Parameters
    ----------
    scores:
        Columns ``lead_id, manager_id, conversion_probability, intent_bucket``.
        One row per eligible pair.
    remaining_capacity:
        ``manager_id -> seats left today`` (``MAX_LEADS_PER_MANAGER`` minus load
        already held). Managers absent from the map are treated as full.
    intent_priority:
        Lower value = higher priority. Defaults to ``INTENT_PRIORITY``.

    Returns
    -------
    One row per *assigned* lead with primary/fallback managers, the primary's
    match score, and a confidence margin. Leads with no capacity-available
    manager are omitted (they overflow to the pool stage).
    """
    if scores.empty:
        return _empty_assignments()

    intent_priority = intent_priority or INTENT_PRIORITY
    lowest = max(intent_priority.values()) + 1

    # Strongest match first within each lead's candidate list.
    ordered = scores.sort_values("conversion_probability", ascending=False)
    grouped = ordered.groupby("lead_id", sort=False)

    candidates: dict[str, list[tuple[str, float]]] = {}
    lead_intent: dict[str, str] = {}
    for lead_id, grp in grouped:
        candidates[lead_id] = list(
            zip(grp["manager_id"], grp["conversion_probability"], strict=False)
        )
        lead_intent[lead_id] = grp["intent_bucket"].iloc[0]

    # Process leads high-intent first, then by their best available score, so
    # H leads claim the strongest managers before lower tiers.
    lead_order = sorted(
        candidates,
        key=lambda lid: (
            intent_priority.get(lead_intent[lid], lowest),
            -candidates[lid][0][1],
        ),
    )

    capacity = dict(remaining_capacity)
    rows: list[dict] = []
    for lead_id in lead_order:
        cands = candidates[lead_id]

        primary = next(((m, float(p)) for m, p in cands if capacity.get(m, 0) > 0), None)
        if primary is None:
            continue  # every scored manager is full -> pool handles this lead
        primary_id, primary_p = primary
        capacity[primary_id] -= 1

        # Fallback: the best *other* manager, preferring one that still has a
        # seat so a decline can actually be routed there.
        fallback = next(
            ((m, float(p)) for m, p in cands if m != primary_id and capacity.get(m, 0) > 0),
            None,
        )
        if fallback is None:
            fallback = next(((m, float(p)) for m, p in cands if m != primary_id), None)

        fallback_id, fallback_p = fallback if fallback else (None, None)
        confidence = primary_p - fallback_p if fallback_p is not None else primary_p

        rows.append(
            {
                "lead_id": lead_id,
                "primary_manager_id": primary_id,
                "fallback_manager_id": fallback_id,
                # Clip: a fallback with no capacity can out-score the primary,
                # which just means the primary won on availability, not margin.
                "confidence_score": max(0.0, min(1.0, confidence)),
                "match_score": primary_p,
                "intent_bucket": lead_intent[lead_id],
            }
        )

    return pd.DataFrame(rows, columns=_ASSIGNMENT_COLUMNS)


def _business_date(event: dict) -> str:
    if event.get("business_date"):
        return str(event["business_date"])
    return str(read_sql("SELECT current_date AS d").iloc[0]["d"])


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("optimizer requires run_id in the event")

    try:
        business_date = _business_date(event)

        scores = read_sql(_SCORED_PAIRS, {"run_id": run_id})
        loads = read_sql(_CURRENT_LOAD, {"business_date": business_date})
        load_by_manager = dict(zip(loads["manager_id"], loads["load"].astype(int), strict=False))

        # Remaining seats per manager that appears in the scored set.
        remaining = {
            manager_id: max(0, MAX_LEADS_PER_MANAGER - load_by_manager.get(manager_id, 0))
            for manager_id in scores["manager_id"].unique()
        }

        assignments = optimize_assignments(scores, remaining)

        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM assignments WHERE run_id = :run_id"), {"run_id": run_id})

        if not assignments.empty:
            assignments = assignments.copy()
            assignments.insert(0, "run_id", run_id)
            assignments["business_date"] = business_date
            write_dataframe(assignments, "assignments")

        leads_scored = int(scores["lead_id"].nunique())
        leads_assigned = int(len(assignments))

        update_run(run_id, stage="optimize", leads_assigned=leads_assigned)

        logger.info(
            "optimizer run=%s date=%s leads_scored=%s leads_assigned=%s overflow=%s",
            run_id, business_date, leads_scored, leads_assigned, leads_scored - leads_assigned,
        )

        return {
            **{k: event[k] for k in ("run_id", "batch_id", "business_date", "model_id") if k in event},
            "business_date": business_date,
            "leads_assigned": leads_assigned,
            "leads_scored": leads_scored,
            "leads_overflow": leads_scored - leads_assigned,
        }
    except Exception as exc:
        logger.exception("optimizer failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="optimize")
        raise
