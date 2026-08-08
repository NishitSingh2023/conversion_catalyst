"""Tests for the loader's chunked reads.

The real files are 1.29 GB / 143 MB, so neither is read whole. Both of these tests
pin the property that makes chunking safe: the result must not depend on where the
chunk boundaries land.
"""
from __future__ import annotations

import pandas as pd

from data.adapt_real_data import build_reference_data, sample_new_leads
from scripts.load_sample_data import read_distinct_new_leads, scan_history_reference

NEW_LEAD_ROWS = [
    {"PROSPECTID": f"lead-{i % 40:03d}", "PRED_CATEGORY_WITH_SALES": ["H", "M", "L", None][i % 4],
     "LEAD_MX_STATE": "Tamil Nadu", "LEAD_CITY": "Chennai",
     "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi", "LEAD_EXAM": "JEE",
     "LEAD_SOURCE": "IL Website", "LEAD_GRADE": f"grade_{(i % 12) + 1}"}
    for i in range(140)
]

HISTORY_ROWS = [
    {"PROSPECTID": f"l{i}", "REP_ID": f"rep-{i % 7}", "REP_NAME": "Someone",
     "LEAD_PREDICTION_CATEGORY": "H", "LEAD_REGION": ["Southern_1", "Northern", "Eastern"][i % 3],
     "LEAD_CITY": ["Chennai", "Delhi", "Kolkata"][i % 3], "LEAD_STATE": "TAMIL NADU",
     "LEAD_STATE_CODE": "TN_0044", "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi",
     "REP_TARGET_EXAM": "JEE", "LEAD_SOURCE": f"src-{i % 9}", "LEAD_GRADE": "10",
     "LEAD_TOTAL_ATTEMPTED_CALLS": 1, "LEAD_CONVERTED_SALE": i % 2,
     "LEAD_GENERATED_DATE": "2026-01-05 10:00:00.000"}
    for i in range(120)
]


def _csv(tmp_path, rows, name):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_chunked_dedupe_matches_reading_the_whole_file(tmp_path):
    path = _csv(tmp_path, NEW_LEAD_ROWS, "leads.csv")
    whole = pd.read_csv(path).drop_duplicates(subset=["PROSPECTID"], keep="first")

    for chunksize in (7, 33, 1_000):
        frame, rows = read_distinct_new_leads(path, chunksize)
        assert rows == len(NEW_LEAD_ROWS)
        assert frame["PROSPECTID"].tolist() == whole["PROSPECTID"].tolist()


def test_sample_is_the_same_however_the_file_was_chunked(tmp_path):
    path = _csv(tmp_path, NEW_LEAD_ROWS, "leads.csv")
    small, _ = read_distinct_new_leads(path, 7)
    large, _ = read_distinct_new_leads(path, 1_000)
    assert sample_new_leads(small, 20).equals(sample_new_leads(large, 20))


def test_reference_scan_matches_an_in_memory_build(tmp_path):
    """Pass 1 reads six columns in chunks; the lookups must be identical to those
    built from the whole frame."""
    path = _csv(tmp_path, HISTORY_ROWS, "history.csv")
    expected = build_reference_data(pd.DataFrame(HISTORY_ROWS))
    scanned = scan_history_reference(path, chunksize=11)
    assert scanned == expected
