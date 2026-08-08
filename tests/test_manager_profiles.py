"""Tests for manager profile derivation.

Two implementations exist of one definition: the pure pandas ``derive_profiles``
(used by training, which already has history in memory) and the SQL
``refresh_manager_profiles`` (used by the pipeline, because the real 2.7M-row
history does not fit the ingest function's 1024MB). The first group of tests
pins the definition without a database; ``test_sql_and_pandas_agree`` runs both
over the same seeded history and asserts they match.
"""
from __future__ import annotations

import math
import uuid
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

from shared.manager_profiles import derive_profiles, refresh_manager_profiles


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # MGR1: two H leads (1 converted), Hindi/Delhi, recent.
            {"manager_id": "MGR1", "lead_intent_bucket": "H", "lead_language": "Hindi",
             "lead_geography": "Delhi", "lead_product": "JEE", "first_response_mins": 10,
             "converted": True, "interaction_date": "2026-08-01"},
            {"manager_id": "MGR1", "lead_intent_bucket": "H", "lead_language": "Hindi",
             "lead_geography": "Delhi", "lead_product": "NEET", "first_response_mins": 20,
             "converted": False, "interaction_date": "2026-08-05"},
            # MGR2: one M lead converted, English/Mumbai, stale (long ago).
            {"manager_id": "MGR2", "lead_intent_bucket": "M", "lead_language": "English",
             "lead_geography": "Mumbai", "lead_product": "CBSE-10", "first_response_mins": 30,
             "converted": True, "interaction_date": "2026-01-01"},
        ]
    )


def test_derive_profiles_basic_aggregates():
    profiles = derive_profiles(_history(), as_of=date(2026, 8, 7), active_window_days=30)
    p = profiles.set_index("manager_id")

    # Coverage lists are sorted-unique.
    assert p.loc["MGR1", "languages_handled"] == ["Hindi"]
    assert p.loc["MGR1", "geographies_handled"] == ["Delhi"]
    assert sorted(p.loc["MGR1", "products_handled"]) == ["JEE", "NEET"]

    # Conversion rates.
    assert p.loc["MGR1", "conv_rate_overall"] == 0.5
    assert p.loc["MGR1", "conv_rate_H"] == 0.5
    assert p.loc["MGR2", "conv_rate_M"] == 1.0
    assert p.loc["MGR1", "total_leads_handled"] == 2


def test_derived_active_flag_uses_window():
    profiles = derive_profiles(_history(), as_of=date(2026, 8, 7), active_window_days=30)
    p = profiles.set_index("manager_id")
    # MGR1 last active 2026-08-05 -> active; MGR2 last active 2026-01-01 -> inactive.
    assert p.loc["MGR1", "derived_active_flag"] is True or p.loc["MGR1", "derived_active_flag"]
    assert not p.loc["MGR2", "derived_active_flag"]


def test_empty_history_returns_empty_frame():
    profiles = derive_profiles(pd.DataFrame())
    assert profiles.empty


def test_coverage_share_is_out_of_non_null_values_not_row_count():
    """The min-support denominator excludes nulls, and that changes the answer.

    One Mumbai lead out of 12 rows that recorded a geography is 8.3% and clears
    the 8% bar; the same lead out of the manager's 25 total rows would be 4% and
    would not. The rule is the former - a manager who only ever logs geography on
    half their leads is not penalised for the missing half.
    """
    geographies = ["Delhi"] * 11 + ["Mumbai"] + [None] * 13
    history = pd.DataFrame(
        [
            {
                "manager_id": "M", "lead_intent_bucket": "H", "lead_language": "Hindi",
                "lead_geography": geography, "lead_product": "JEE",
                "first_response_mins": 10, "converted": False,
                "interaction_date": "2026-08-01",
            }
            for geography in geographies
        ]
    )
    assert len(history) == 25
    assert history["lead_geography"].notna().sum() == 12, "1/12 = 8.3%, 1/25 = 4%"

    profiles = derive_profiles(history, as_of=date(2026, 8, 7), min_coverage_support=0.08)
    assert profiles.loc[0, "geographies_handled"] == ["Delhi", "Mumbai"]


# ---------------------------------------------------------------------------
# SQL / pandas equivalence
# ---------------------------------------------------------------------------
# The profile definition has two implementations (see the module docstring), so
# they are checked against each other over one seeded history rather than each
# being trusted separately.

AS_OF = date(2026, 8, 7)
ACTIVE_DAYS = 30

_RATE_COLUMNS = ["conv_rate_overall", "conv_rate_H", "conv_rate_M", "conv_rate_L"]
_ARRAY_COLUMNS = ["languages_handled", "geographies_handled", "products_handled"]
_EXACT_COLUMNS = [
    "manager_name", "total_leads_handled", "last_active_date", "derived_active_flag",
]


def _seeded_history(prefix: str) -> pd.DataFrame:
    """History chosen to exercise every place the two implementations could differ."""
    rows = []

    # A: the coverage-denominator case. 25 rows, only 12 with a geography, so the
    # single Mumbai lead is 1/12 = 8.3% and survives the 8% bar. Also: no L-intent
    # rows (rate must be 0.0, not null), first_response_mins null throughout
    # (pandas NaN vs SQL NULL), and two names tied 12-12 (tie goes to 'Ada').
    a = f"{prefix}_A"
    geos = ["Delhi"] * 11 + ["Mumbai"] + [None] * 13
    for i, geo in enumerate(geos):
        rows.append(
            {
                "lead_id": f"{a}-{i}", "manager_id": a,
                "manager_name": "Ada" if i < 12 else ("Zed" if i < 24 else None),
                "lead_intent_bucket": "H" if i % 2 else "M",
                "lead_language": "Hindi" if i < 24 else None,
                "lead_geography": geo,
                "lead_product": "JEE",
                "lead_source": "organic", "lead_grade": "11",
                "first_response_mins": None,
                "converted": i % 3 == 0,
                "interaction_date": "2026-08-05",
            }
        )

    # B: a single row. Every coverage list is either the one value or empty, every
    # intent rate but its own is the 0.0 default, and it is outside the activity
    # window so derived_active_flag is False.
    b = f"{prefix}_B"
    rows.append(
        {
            "lead_id": f"{b}-0", "manager_id": b, "manager_name": "Bob",
            "lead_intent_bucket": "L", "lead_language": None,
            "lead_geography": "Pune", "lead_product": "NEET",
            "lead_source": "ads", "lead_grade": "12",
            "first_response_mins": 30.0, "converted": False,
            "interaction_date": "2026-01-01",
        }
    )

    # C: response time present on some rows only - pandas skips NaN, SQL skips
    # NULL, and both must land on the mean of the two recorded values.
    c = f"{prefix}_C"
    for i, (mins, conv, bucket) in enumerate(
        [(10.0, True, "H"), (None, False, "H"), (20.0, True, "M"), (None, False, "L")]
    ):
        rows.append(
            {
                "lead_id": f"{c}-{i}", "manager_id": c, "manager_name": "Cyd",
                "lead_intent_bucket": bucket, "lead_language": "Tamil",
                "lead_geography": "Chennai", "lead_product": "CBSE-10",
                "lead_source": "referral", "lead_grade": "10",
                "first_response_mins": mins, "converted": conv,
                "interaction_date": "2026-07-20",
            }
        )

    return pd.DataFrame(rows)


@pytest.fixture
def seeded_history(db):
    """Load one history fixture into Postgres under unique manager ids.

    Ids are unique per run so the comparison is unaffected by whatever else is in
    the table: every aggregate is per-manager, so other managers' rows cannot
    change these managers' profiles.
    """
    from shared.db import write_dataframe

    prefix = f"P{uuid.uuid4().hex[:8]}"
    history = _seeded_history(prefix)
    write_dataframe(history, "lead_manager_history")

    yield prefix, history

    managers = sorted(history["manager_id"].unique())
    with db.begin() as conn:
        conn.execute(
            text("DELETE FROM lead_manager_history WHERE manager_id = ANY(:ids)"),
            {"ids": managers},
        )
        conn.execute(
            text("DELETE FROM manager_profiles WHERE manager_id = ANY(:ids)"),
            {"ids": managers},
        )


def _profiles_from_db(db, managers: list[str]) -> pd.DataFrame:
    """Read back the SQL-written profiles, undoing Postgres's lower-casing.

    ``conv_rate_H`` and friends are declared unquoted in the DDL, so the database
    stores them lower-cased.
    """
    from shared.db import read_sql

    rename = {c.lower(): c for c in _RATE_COLUMNS if c.lower() != c}
    got = read_sql(
        "SELECT * FROM manager_profiles WHERE manager_id = ANY(:ids) ORDER BY manager_id",
        {"ids": managers},
    ).rename(columns=rename)
    return got.set_index("manager_id")


def _same_response_mins(sql_value, pandas_value) -> bool:
    """NULL from ``avg()`` and NaN from ``Series.mean()`` both mean "no data".

    Neither implementation can produce a number here when every input row is
    null, and ``build_features`` fills both with 0.0, so they are equivalent. The
    real dataset has ``first_response_mins`` null on all 2.7M rows, so this is the
    common case rather than a corner.
    """
    sql_missing = sql_value is None or (
        isinstance(sql_value, float) and math.isnan(sql_value)
    )
    pandas_missing = pandas_value is None or (
        isinstance(pandas_value, float) and math.isnan(pandas_value)
    )
    if sql_missing or pandas_missing:
        return sql_missing and pandas_missing
    return math.isclose(float(sql_value), float(pandas_value), rel_tol=1e-9)


def test_sql_and_pandas_agree(db, seeded_history):
    """refresh_manager_profiles (SQL) must reproduce derive_profiles (pandas)."""
    prefix, history = seeded_history
    managers = sorted(history["manager_id"].unique())

    written = refresh_manager_profiles(as_of=AS_OF, active_window_days=ACTIVE_DAYS)
    assert written >= len(managers)

    got = _profiles_from_db(db, managers)
    expected = derive_profiles(
        history, as_of=AS_OF, active_window_days=ACTIVE_DAYS
    ).set_index("manager_id")

    assert list(got.index) == managers
    assert list(got.index) == list(expected.index), "same managers, same order"

    for manager_id in managers:
        sql_row, pandas_row = got.loc[manager_id], expected.loc[manager_id]

        for column in _ARRAY_COLUMNS:
            assert list(sql_row[column]) == list(pandas_row[column]), (
                f"{manager_id}.{column}"
            )
        for column in _RATE_COLUMNS:
            assert float(sql_row[column]) == pytest.approx(
                float(pandas_row[column]), rel=1e-9, abs=1e-12
            ), f"{manager_id}.{column}"
        for column in _EXACT_COLUMNS:
            assert sql_row[column] == pandas_row[column], f"{manager_id}.{column}"

        assert _same_response_mins(
            sql_row["avg_response_mins"], pandas_row["avg_response_mins"]
        ), f"{manager_id}.avg_response_mins"


def test_sql_refresh_is_idempotent(db, seeded_history):
    """A second refresh updates in place rather than duplicating or drifting."""
    prefix, history = seeded_history
    managers = sorted(history["manager_id"].unique())

    refresh_manager_profiles(as_of=AS_OF, active_window_days=ACTIVE_DAYS)
    first = _profiles_from_db(db, managers)
    refresh_manager_profiles(as_of=AS_OF, active_window_days=ACTIVE_DAYS)
    second = _profiles_from_db(db, managers)

    assert list(first.index) == list(second.index)
    for column in _RATE_COLUMNS + _ARRAY_COLUMNS + _EXACT_COLUMNS:
        assert list(first[column]) == list(second[column]), column
