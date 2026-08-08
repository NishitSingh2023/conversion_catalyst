"""Tests for the COPY bulk-load path.

History is 2.7M rows. Loading it through ``write_dataframe`` is 544 INSERT round
trips, so the loader streams adapted chunks into COPY instead. COPY hands the
parsing to Postgres, which means the *encoding* of nulls and booleans is now this
module's responsibility rather than the driver's - that is what these tests pin
down, against a real table with the real column types.
"""
from __future__ import annotations

import math

import pandas as pd
import psycopg2
import pytest
from sqlalchemy import text

from shared.db import copy_dataframe, copy_dataframes

SCRATCH = "copy_bulk_load_scratch"


@pytest.fixture
def scratch(db):
    """A throwaway table with lead_manager_history's exact column types."""
    with db.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {SCRATCH}"))
        conn.execute(
            text(f"CREATE TABLE {SCRATCH} (LIKE lead_manager_history INCLUDING DEFAULTS)")
        )
    yield db
    with db.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {SCRATCH}"))


COLUMNS = [
    "lead_id",
    "manager_id",
    "manager_name",
    "lead_intent_bucket",
    "lead_geography",
    "lead_language",
    "lead_product",
    "lead_source",
    "lead_grade",
    "contact_attempts",
    "first_response_mins",
    "converted",
    "interaction_date",
]


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def _row(**overrides) -> dict:
    row = {
        "lead_id": "l1",
        "manager_id": "m1",
        "manager_name": "Agent-0001",
        "lead_intent_bucket": "H",
        "lead_geography": "South",
        "lead_language": "Hindi",
        "lead_product": "JEE",
        "lead_source": "IL Website",
        "lead_grade": "10",
        "contact_attempts": 3,
        "first_response_mins": float("nan"),
        "converted": False,
        "interaction_date": "2026-01-05",
    }
    row.update(overrides)
    return row


def _read(db) -> pd.DataFrame:
    with db.connect() as conn:
        return pd.read_sql(text(f"SELECT * FROM {SCRATCH} ORDER BY lead_id"), conn)


def test_copy_round_trips_values_nulls_and_booleans(scratch):
    frame = _frame(
        [
            _row(lead_id="l1", converted=True, first_response_mins=12.5),
            # NaN/None must land as SQL NULL, not the string "nan" or "".
            _row(lead_id="l2", converted=False, lead_geography=None, lead_grade=None),
        ]
    )
    assert copy_dataframe(frame, SCRATCH, columns=COLUMNS) == 2

    out = _read(scratch)
    assert out["converted"].tolist() == [True, False]
    assert out.loc[0, "first_response_mins"] == 12.5
    assert math.isnan(out.loc[1, "first_response_mins"])
    assert out.loc[1, "lead_geography"] is None
    assert out.loc[1, "lead_grade"] is None
    assert out.loc[0, "interaction_date"].isoformat() == "2026-01-05"


def test_copy_survives_delimiters_and_quotes_in_text(scratch):
    """Free-ish text columns (source, manager_name) reach COPY unescaped."""
    nasty = 'Referral, "partner" \\ site'
    copy_dataframe(_frame([_row(lead_source=nasty)]), SCRATCH, columns=COLUMNS)
    assert _read(scratch).loc[0, "lead_source"] == nasty


def test_copy_streams_chunks_into_one_transaction(scratch):
    """The loader hands COPY a generator; nothing may be materialised twice."""
    consumed = []

    def chunks():
        for i in range(4):
            consumed.append(i)
            yield _frame([_row(lead_id=f"l{i}{j}") for j in range(3)])

    written = copy_dataframes(chunks(), SCRATCH, columns=COLUMNS, buffer_rows=2)
    assert written == 12
    assert consumed == [0, 1, 2, 3]
    assert len(_read(scratch)) == 12


def test_a_failed_chunk_rolls_the_whole_load_back(scratch):
    """Better an empty table than a half-loaded history."""
    def chunks():
        yield _frame([_row(lead_id="good")])
        yield _frame([_row(lead_id="bad", contact_attempts="not-a-number")])

    with pytest.raises(psycopg2.DataError):
        copy_dataframes(chunks(), SCRATCH, columns=COLUMNS)
    assert _read(scratch).empty


def test_truncate_and_reload_share_the_transaction(scratch):
    copy_dataframe(_frame([_row(lead_id="old")]), SCRATCH, columns=COLUMNS)
    copy_dataframe(_frame([_row(lead_id="new")]), SCRATCH, columns=COLUMNS, truncate=True)
    assert _read(scratch)["lead_id"].tolist() == ["new"]


def test_identifiers_are_not_interpolated_blindly(scratch):
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        copy_dataframe(_frame([_row()]), f"{SCRATCH}; DROP TABLE {SCRATCH}")
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        copy_dataframe(_frame([_row()]).rename(columns={"lead_id": "lead_id)--"}), SCRATCH)
