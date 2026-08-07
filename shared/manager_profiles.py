"""Derive manager profiles from lead_manager_history.

There is no standalone managers table in this system: every manager attribute is
computed from the historical (lead, manager, converted?) triples. This module
turns that history into a per-manager profile (coverage + conversion rates +
activity) that the eligibility filter and the scoring features both consume.

The aggregation is deliberately expressed as pandas over a history DataFrame so
it is unit-testable in isolation and reusable both in the pipeline (reading from
Postgres) and in tests (reading from a fixture frame).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from shared.constants import ACTIVE_WINDOW_DAYS


def _covered_values(values: pd.Series, min_support: float) -> list[str]:
    """Return the values a manager genuinely covers.

    A value counts as "covered" only if it accounts for at least ``min_support``
    of the manager's interactions. This filters out the occasional off-patch
    lead (e.g. one Tamil lead handled by a Hindi-focused rep) so the derived
    coverage reflects where the manager actually operates rather than every
    category they have ever touched once.
    """
    clean = values.dropna()
    if clean.empty:
        return []
    shares = clean.value_counts(normalize=True)
    return sorted(shares[shares >= min_support].index.tolist())


def _conv_rate(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return float(df["converted"].mean())


def _modal_name(grp: pd.DataFrame) -> str | None:
    """Most frequent non-null manager_name in the group, if the column exists.

    History rows can disagree on a manager's name (whitespace, the odd typo), so
    the profile takes the majority spelling. Absent entirely (e.g. synthetic
    fixtures with no name column) it falls back to None and callers use the id.
    """
    if "manager_name" not in grp.columns:
        return None
    names = grp["manager_name"].dropna()
    if names.empty:
        return None
    return str(names.mode().iloc[0])


def derive_profiles(
    history: pd.DataFrame,
    as_of: date | None = None,
    active_window_days: int = ACTIVE_WINDOW_DAYS,
    min_coverage_support: float = 0.08,
) -> pd.DataFrame:
    """Aggregate ``lead_manager_history`` into one row per manager.

    Parameters
    ----------
    history:
        DataFrame with at least the columns ``manager_id``,
        ``lead_intent_bucket``, ``lead_language``, ``lead_geography``,
        ``lead_product``, ``first_response_mins``, ``converted`` and
        ``interaction_date``.
    as_of:
        Reference date for the activity window. Defaults to today.
    active_window_days:
        A manager is flagged active if their most recent interaction is within
        this many days of ``as_of``.

    Returns
    -------
    DataFrame keyed by ``manager_id`` with the columns of the
    ``manager_profiles`` table (minus ``refreshed_at``).
    """
    if history.empty:
        return pd.DataFrame(
            columns=[
                "manager_id", "manager_name", "languages_handled",
                "geographies_handled", "products_handled", "conv_rate_overall",
                "conv_rate_H", "conv_rate_M", "conv_rate_L", "avg_response_mins",
                "total_leads_handled", "last_active_date", "derived_active_flag",
            ]
        )

    as_of = as_of or date.today()
    hist = history.copy()
    hist["converted"] = hist["converted"].astype(bool)
    hist["interaction_date"] = pd.to_datetime(hist["interaction_date"]).dt.date
    active_cutoff = as_of - timedelta(days=active_window_days)

    rows = []
    for manager_id, grp in hist.groupby("manager_id"):
        last_active = grp["interaction_date"].max()
        rows.append(
            {
                "manager_id": manager_id,
                "manager_name": _modal_name(grp),
                "languages_handled": _covered_values(grp["lead_language"], min_coverage_support),
                "geographies_handled": _covered_values(grp["lead_geography"], min_coverage_support),
                "products_handled": _covered_values(grp["lead_product"], min_coverage_support),
                "conv_rate_overall": _conv_rate(grp),
                "conv_rate_H": _conv_rate(grp[grp["lead_intent_bucket"] == "H"]),
                "conv_rate_M": _conv_rate(grp[grp["lead_intent_bucket"] == "M"]),
                "conv_rate_L": _conv_rate(grp[grp["lead_intent_bucket"] == "L"]),
                "avg_response_mins": float(grp["first_response_mins"].mean()),
                "total_leads_handled": int(len(grp)),
                "last_active_date": last_active,
                "derived_active_flag": bool(last_active >= active_cutoff),
            }
        )

    return pd.DataFrame(rows).sort_values("manager_id").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Persistence helpers (DB-touching; kept separate from the pure aggregation).
# ---------------------------------------------------------------------------
_HISTORY_QUERY = """
SELECT manager_id, manager_name, lead_intent_bucket, lead_language,
       lead_geography, lead_product, first_response_mins, converted,
       interaction_date
FROM lead_manager_history
"""

_UPSERT = """
INSERT INTO manager_profiles (
    manager_id, manager_name, languages_handled, geographies_handled,
    products_handled, conv_rate_overall, conv_rate_H, conv_rate_M, conv_rate_L,
    avg_response_mins, total_leads_handled, last_active_date,
    derived_active_flag, refreshed_at
) VALUES (
    :manager_id, :manager_name, :languages_handled, :geographies_handled,
    :products_handled, :conv_rate_overall, :conv_rate_H, :conv_rate_M,
    :conv_rate_L, :avg_response_mins, :total_leads_handled, :last_active_date,
    :derived_active_flag, now()
)
ON CONFLICT (manager_id) DO UPDATE SET
    manager_name        = EXCLUDED.manager_name,
    languages_handled   = EXCLUDED.languages_handled,
    geographies_handled = EXCLUDED.geographies_handled,
    products_handled    = EXCLUDED.products_handled,
    conv_rate_overall   = EXCLUDED.conv_rate_overall,
    conv_rate_H         = EXCLUDED.conv_rate_H,
    conv_rate_M         = EXCLUDED.conv_rate_M,
    conv_rate_L         = EXCLUDED.conv_rate_L,
    avg_response_mins   = EXCLUDED.avg_response_mins,
    total_leads_handled = EXCLUDED.total_leads_handled,
    last_active_date    = EXCLUDED.last_active_date,
    derived_active_flag = EXCLUDED.derived_active_flag,
    refreshed_at        = now()
"""


def refresh_manager_profiles(as_of: date | None = None) -> int:
    """Read history from Postgres, derive profiles, and upsert them.

    Returns the number of manager profiles written. Called at the start of each
    pipeline run so eligibility and scoring see fresh, materialised profiles.
    """
    from sqlalchemy import text

    from shared.db import get_engine, read_sql

    history = read_sql(_HISTORY_QUERY)
    profiles = derive_profiles(history, as_of=as_of)
    if profiles.empty:
        return 0

    records = profiles.to_dict(orient="records")
    # psycopg2 adapts Python lists to Postgres arrays automatically.
    with get_engine().begin() as conn:
        conn.execute(text(_UPSERT), records)
    return len(records)
