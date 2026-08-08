"""Derive manager profiles from lead_manager_history.

There is no standalone managers table in this system: every manager attribute is
computed from the historical (lead, manager, converted?) triples. This module
turns that history into a per-manager profile (coverage + conversion rates +
activity) that the eligibility filter and the scoring features both consume.

There are two implementations of the same definition, for two different callers:

* :func:`derive_profiles` - pure pandas over a history DataFrame. Training already
  holds the history in memory to build its feature matrix, so it derives profiles
  from that frame; it is also what makes the aggregation unit-testable without a
  database.
* :func:`refresh_manager_profiles` - the same aggregation as SQL, used by the
  pipeline. The real history is 2.7M rows, which does not fit the ingest
  function's 1024MB, so nothing but the resulting row count crosses the network.

They must agree. ``tests/test_manager_profiles.py`` seeds one history and asserts
both produce the same managers, arrays and rates, so a change to one that is not
mirrored in the other fails the suite rather than silently shifting eligibility.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from shared.constants import ACTIVE_WINDOW_DAYS, MANAGER_NUMERIC_FEATURES

# Minimum share of a manager's non-null values for an attribute before that value
# counts as "covered". Defined once because the pandas and SQL implementations
# must apply the identical threshold - see ``_covered_values`` for the semantics
# and ``_REFRESH_SQL`` for the SQL mirror of them.
DEFAULT_MIN_COVERAGE_SUPPORT = 0.08


def _covered_values(values: pd.Series, min_support: float) -> list[str]:
    """Return the values a manager genuinely covers.

    A value counts as "covered" only if it accounts for at least ``min_support``
    of the manager's interactions. This filters out the occasional off-patch
    lead (e.g. one Tamil lead handled by a Hindi-focused rep) so the derived
    coverage reflects where the manager actually operates rather than every
    category they have ever touched once.

    Note the denominator: nulls are dropped *before* the shares are computed, so
    a value's share is out of the manager's non-null values for this attribute,
    not out of their total row count. That distinction is load-bearing on the real
    data - 5.5% of history rows have no geography - and the SQL mirror of this
    function in ``_REFRESH_SQL`` reproduces it deliberately.
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
    min_coverage_support: float = DEFAULT_MIN_COVERAGE_SUPPORT,
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
# Persistence (DB-touching; kept separate from the pure aggregation above).
# ---------------------------------------------------------------------------
# This used to read all of lead_manager_history into pandas and call
# ``derive_profiles``. That does not survive the real dataset: 2.7M history rows
# do not fit the ingest function's 1024MB, and the per-manager Python loop is the
# slowest possible way to compute nine aggregates. The aggregation is now pushed
# into Postgres so nothing but the row count crosses the network.
#
# ``derive_profiles`` above stays exactly as it was - training builds profiles
# from an in-memory frame and never touches the database - so the two are
# separate implementations of one definition. ``tests/test_manager_profiles.py``
# runs both over the same seeded history and asserts they agree; change one and
# that test tells you to change the other.
#
# The subtle parts, in the order they bite:
#
# * **Coverage denominator.** ``_covered_values`` drops nulls before computing
#   shares, so a value needs ``min_support`` of the manager's *non-null* values
#   for that attribute - not of their total rows. The ``WHERE value IS NOT NULL``
#   below sits upstream of the ``sum(n) OVER (...)`` window for exactly that
#   reason. Using the row count instead would silently shrink coverage lists on
#   the real data, where 5.5% of rows carry no geography, and quietly change who
#   is eligible for what.
# * **Array ordering.** ``_covered_values`` returns Python ``sorted()``, i.e.
#   codepoint order. The database's default collation is en_US.utf8, which orders
#   differently (it ignores case and punctuation), so the aggregate is ordered
#   ``COLLATE "C"`` to reproduce Python's ordering. Eligibility only ever tests
#   membership with ``= ANY(...)``, so order is cosmetic downstream - but without
#   this the equivalence test compares differently-ordered arrays.
# * **Empty intent buckets.** ``_conv_rate`` returns 0.0 for an empty group, so
#   the FILTERed averages are COALESCEd to 0.0 rather than left NULL.
# * **avg_response_mins.** ``avg()`` over all-NULL input returns NULL where pandas
#   returns NaN. Both mean "no response data" and both become 0.0 at
#   ``build_features``' ``fillna``, so they are treated as equivalent. This is not
#   hypothetical: ``first_response_mins`` is NULL on every row of the real data.
# * **Modal name.** ``_modal_name`` takes ``mode().iloc[0]``, which breaks ties on
#   the lexicographically smallest name; ``DISTINCT ON`` with the same ordering
#   matches it.
_REFRESH_SQL = """
WITH agg AS (
    SELECT
        manager_id,
        avg(CASE WHEN converted THEN 1.0::double precision
                 ELSE 0.0::double precision END)              AS conv_rate_overall,
        COALESCE(avg(CASE WHEN converted THEN 1.0::double precision
                          ELSE 0.0::double precision END)
                 FILTER (WHERE lead_intent_bucket = 'H'), 0.0) AS conv_rate_h,
        COALESCE(avg(CASE WHEN converted THEN 1.0::double precision
                          ELSE 0.0::double precision END)
                 FILTER (WHERE lead_intent_bucket = 'M'), 0.0) AS conv_rate_m,
        COALESCE(avg(CASE WHEN converted THEN 1.0::double precision
                          ELSE 0.0::double precision END)
                 FILTER (WHERE lead_intent_bucket = 'L'), 0.0) AS conv_rate_l,
        avg(first_response_mins)                              AS avg_response_mins,
        count(*)::int                                         AS total_leads_handled,
        max(interaction_date)                                 AS last_active_date
    FROM lead_manager_history
    GROUP BY manager_id
),
-- One scan, unpivoted into (manager, attribute, value) so the three coverage
-- lists share a single pass over the history table.
attr_values AS (
    SELECT h.manager_id, a.attr, a.value
    FROM lead_manager_history h
    CROSS JOIN LATERAL (VALUES
        ('language',  h.lead_language),
        ('geography', h.lead_geography),
        ('product',   h.lead_product)
    ) AS a(attr, value)
    WHERE a.value IS NOT NULL          -- denominator = non-null values only
),
attr_counts AS (
    SELECT manager_id, attr, value, count(*) AS n
    FROM attr_values
    GROUP BY manager_id, attr, value
),
attr_shares AS (
    SELECT
        manager_id, attr, value,
        n::double precision / sum(n) OVER (PARTITION BY manager_id, attr) AS share
    FROM attr_counts
),
covered AS (
    SELECT manager_id, attr, array_agg(value ORDER BY value COLLATE "C") AS vals
    FROM attr_shares
    WHERE share >= :min_support
    GROUP BY manager_id, attr
),
coverage AS (
    SELECT
        manager_id,
        COALESCE(max(vals) FILTER (WHERE attr = 'language'),
                 '{}'::text[]) AS languages_handled,
        COALESCE(max(vals) FILTER (WHERE attr = 'geography'),
                 '{}'::text[]) AS geographies_handled,
        COALESCE(max(vals) FILTER (WHERE attr = 'product'),
                 '{}'::text[]) AS products_handled
    FROM covered
    GROUP BY manager_id
),
modal_name AS (
    SELECT DISTINCT ON (manager_id) manager_id, manager_name
    FROM (
        SELECT manager_id, manager_name, count(*) AS n
        FROM lead_manager_history
        WHERE manager_name IS NOT NULL
        GROUP BY manager_id, manager_name
    ) named
    ORDER BY manager_id, n DESC, manager_name COLLATE "C"
)
INSERT INTO manager_profiles (
    manager_id, manager_name, languages_handled, geographies_handled,
    products_handled, conv_rate_overall, conv_rate_H, conv_rate_M, conv_rate_L,
    avg_response_mins, total_leads_handled, last_active_date,
    derived_active_flag, refreshed_at
)
SELECT
    a.manager_id,
    n.manager_name,
    COALESCE(c.languages_handled,   '{}'::text[]),
    COALESCE(c.geographies_handled, '{}'::text[]),
    COALESCE(c.products_handled,    '{}'::text[]),
    a.conv_rate_overall, a.conv_rate_h, a.conv_rate_m, a.conv_rate_l,
    a.avg_response_mins, a.total_leads_handled, a.last_active_date,
    a.last_active_date >= :active_cutoff,
    now()
FROM agg a
LEFT JOIN coverage   c ON c.manager_id = a.manager_id
LEFT JOIN modal_name n ON n.manager_id = a.manager_id
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


def refresh_manager_profiles(
    as_of: date | None = None,
    active_window_days: int = ACTIVE_WINDOW_DAYS,
    min_coverage_support: float = DEFAULT_MIN_COVERAGE_SUPPORT,
) -> int:
    """Aggregate history into ``manager_profiles`` entirely inside Postgres.

    Same definition as :func:`derive_profiles`, expressed as SQL so the 2.7M-row
    history never enters this process. Returns the number of manager profiles
    written. Called at the start of each pipeline run so eligibility and scoring
    see fresh, materialised profiles.

    Managers absent from history are left alone rather than deleted, matching the
    previous upsert-only behaviour.
    """
    from sqlalchemy import text

    from shared.db import get_engine

    as_of = as_of or date.today()
    with get_engine().begin() as conn:
        result = conn.execute(
            text(_REFRESH_SQL),
            {
                "min_support": min_coverage_support,
                "active_cutoff": as_of - timedelta(days=active_window_days),
            },
        )
    return int(result.rowcount)


# ``conv_rate_H`` / ``_M`` / ``_L`` are declared unquoted in the DDL, so Postgres
# folds them to lower case and a SELECT returns ``conv_rate_h``. Everything that
# builds features expects the ``MANAGER_NUMERIC_FEATURES`` spelling that
# ``derive_profiles`` produces, so the read path maps them back.
_STORED_TO_FEATURE_CASE = {
    column.lower(): column
    for column in MANAGER_NUMERIC_FEATURES
    if column.lower() != column
}


def read_manager_profiles() -> pd.DataFrame:
    """Load the materialised profiles as a ``derive_profiles``-shaped frame.

    Both scoring and training read profiles through here, so a model is trained
    on the exact same profile values that inference will see - the profiles table
    is the single materialisation, and neither side re-derives it.
    """
    from shared.db import read_sql

    profiles = read_sql("SELECT * FROM manager_profiles")
    rename = {
        stored: feature
        for stored, feature in _STORED_TO_FEATURE_CASE.items()
        if stored in profiles.columns
    }
    if rename:
        profiles = profiles.rename(columns=rename)

    # An all-NULL numeric column arrives as object dtype holding None, which
    # ``build_features`` would then fillna on an object column - a deprecated
    # pandas path, and not the float column ``derive_profiles`` hands it. Coerce
    # so both routes into the feature builder look identical. Not hypothetical:
    # ``avg_response_mins`` is null for every manager on the real dataset because
    # the source data carries no response times.
    for column in MANAGER_NUMERIC_FEATURES:
        if column in profiles.columns:
            profiles[column] = pd.to_numeric(profiles[column], errors="coerce")
    return profiles
