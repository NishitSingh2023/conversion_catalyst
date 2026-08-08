"""Tests for the real-data adapter's normalisation and batch sizing.

These cover the parts of the load that decide whether the pipeline works at all
on the real 2.7M-row dataset rather than merely runs:

  * geography is a canonical REGION, resolved from the same history-derived
    lookups on both sides (a hard eligibility filter reads it);
  * the dirty columns (grade, source) are bounded, so the one-hot encoding is;
  * the demo batch is capped without losing the intent mix.

Every raw value asserted on here was observed in
``data/sample/lead_rep_dataset.csv`` / ``leads_dataset_HML.csv``.
"""
from __future__ import annotations

import pandas as pd
import pytest

from data.adapt_real_data import (
    CANONICAL_REGIONS,
    OTHER_SOURCE,
    SOURCE_CAP,
    ReferenceBuilder,
    adapt_history,
    adapt_new_leads,
    build_reference_data,
    city_lookup_key,
    intent_mix,
    normalize_grade,
    normalize_language,
    normalize_region,
    sample_new_leads,
    state_lookup_key,
)

# --- normalize_region -----------------------------------------------------

# Every distinct LEAD_REGION spelling in the history file once the ``_1234``
# territory suffix is stripped, with the row count it carries, and where it lands.
OBSERVED_REGIONS = [
    ("Southern", 646272, "South"),
    ("Northern", 338342, "North"),
    ("North & South", 293685, "North & South"),
    ("North", 201072, "North"),
    ("Eastern", 200940, "East"),
    ("Western", 186782, "West"),
    ("South", 133241, "South"),
    ("Central", 75582, "Central"),
    ("North Eastern", 42979, "North East"),
    ("Assamern", 3020, "North East"),
    ("Special (Island UTs)", 86, "Islands"),
    ("West", 76, "West"),
    ("Northern\xa0\u200b", 31, "North"),
    ("North-Eastern", 16, "North East"),
    ("Eastern\xa0\u200b", 16, "East"),
    ("Southern\xa0\u200b", 10, "South"),
    ("Western\xa0\u200b", 9, "West"),
    ("East", 3, "East"),
    ("Central\xa0\u200b", 3, "Central"),
    ("North-Eastern\xa0\u200b", 1, "North East"),
]


@pytest.mark.parametrize(("raw", "_count", "expected"), OBSERVED_REGIONS)
def test_every_observed_region_maps_to_a_canonical_value(raw, _count, expected):
    assert normalize_region(raw) == expected
    assert expected in CANONICAL_REGIONS


def test_numeric_suffix_is_stripped():
    """3,761 raw LEAD_REGION values are 20 regions plus a per-territory suffix."""
    assert normalize_region("Southern_1423") == "South"
    assert normalize_region("North & South_7") == "North & South"
    # Only a trailing suffix, and only when it is underscore + digits.
    assert normalize_region("Southern_extra") is None


def test_invisible_characters_do_not_create_a_second_region():
    assert normalize_region("Northern\xa0\u200b") == normalize_region("Northern")
    assert normalize_region("Northern_12\u200b") == "North"


def test_typo_and_split_regions():
    # "Assamern" is a misspelling of a region that exists properly; Assam is in
    # the north east.
    assert normalize_region("Assamern") == "North East"
    # "North & South" is 294k rows and is kept as itself rather than guessed away.
    assert normalize_region("North & South") == "North & South"


def test_unrecognised_region_is_none_not_a_bucket():
    """Geography is a hard filter, so there is no catch-all region."""
    for value in ("Kathmandu", "??", "", None, float("nan"), "unknown", 42):
        assert normalize_region(value) is None


# --- city / state -> region lookups ---------------------------------------


def _history_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Bengaluru is Southern on two rows, Northern on one -> modal Southern.
            {"LEAD_CITY": "Bengaluru", "LEAD_REGION": "Southern_11",
             "LEAD_STATE": "KARNATAKA", "LEAD_STATE_CODE": "KA_0120",
             "LEAD_SOURCE": "External Database", "REP_ID": "rep-1"},
            {"LEAD_CITY": "bengaluru ", "LEAD_REGION": "Southern",
             "LEAD_STATE": "karnataka", "LEAD_STATE_CODE": "KA",
             "LEAD_SOURCE": "External Database", "REP_ID": "rep-1"},
            {"LEAD_CITY": "BENGALURU", "LEAD_REGION": "Northern",
             "LEAD_STATE": "Karnataka", "LEAD_STATE_CODE": "KA",
             "LEAD_SOURCE": "IL Website", "REP_ID": "rep-2"},
            {"LEAD_CITY": "Guwahati", "LEAD_REGION": "Assamern",
             "LEAD_STATE": "ASSAM", "LEAD_STATE_CODE": "AS_0007",
             "LEAD_SOURCE": "Referral", "REP_ID": "rep-2"},
            # An unrecognised region contributes nothing to the lookup.
            {"LEAD_CITY": "Nowhere", "LEAD_REGION": "Atlantis",
             "LEAD_STATE": "ATLANTIS", "LEAD_STATE_CODE": "AT",
             "LEAD_SOURCE": "Referral", "REP_ID": "rep-3"},
            {"LEAD_CITY": "Chennai", "LEAD_REGION": "Southern",
             "LEAD_STATE": "Tamil Nadu", "LEAD_STATE_CODE": "TN_0044",
             "LEAD_SOURCE": "IL Website", "REP_ID": "rep-3"},
        ]
    )


def test_city_lookup_takes_the_modal_region():
    reference = build_reference_data(_history_rows())
    assert reference.region_for_city("Bengaluru") == "South"
    assert reference.region_for_city("Guwahati") == "North East"


def test_city_lookup_key_is_case_and_whitespace_insensitive():
    assert city_lookup_key("  bengaluru ") == city_lookup_key("BENGALURU") == "BENGALURU"
    reference = build_reference_data(_history_rows())
    for spelling in ("Chennai", "chennai", " CHENNAI ", "Chennai\xa0"):
        assert reference.region_for_city(spelling) == "South"


def test_city_lookup_ignores_unrecognised_regions():
    reference = build_reference_data(_history_rows())
    assert reference.region_for_city("Nowhere") is None
    assert reference.region_for_city("never seen") is None


def test_state_lookup_absorbs_the_separator_and_suffix_variants():
    """LEAD_MX_STATE says "tamil_nadu"/"TAMIL NADU"; LEAD_STATE_CODE says "TN_0044"."""
    reference = build_reference_data(_history_rows())
    for spelling in ("Tamil Nadu", "tamil_nadu", "TAMILNADU", "TN", "TN_0044"):
        assert reference.region_for_state(spelling) == "South"
    assert state_lookup_key("AP_0120") == "AP"
    assert reference.region_for_state("Sealand") is None


def test_reference_can_be_built_from_chunks():
    """The 1.29 GB history file is scanned in chunks; the result must not depend
    on where the chunk boundaries fall."""
    rows = _history_rows()
    whole = build_reference_data(rows)
    builder = ReferenceBuilder()
    for start in range(0, len(rows), 2):
        builder.add(rows.iloc[start : start + 2])
    chunked = builder.build()
    assert chunked.city_to_region == whole.city_to_region
    assert chunked.state_to_region == whole.state_to_region
    assert chunked.allowed_sources == whole.allowed_sources
    assert chunked.manager_ids == whole.manager_ids


# --- grade ----------------------------------------------------------------

JUNK_GRADES = [
    "08000900",
    "0" * 40 + "\u2070\u2070",  # 40 zeros plus unicode superscripts
    "919640361652919640361652",
    "1011",
    "810",
    "14",
    "16",
    "0",
    "2026",
    "TOMAR CHILDREN SCHOOL",
    "Dropper",
    "Foundation",
]


@pytest.mark.parametrize("raw", JUNK_GRADES)
def test_junk_grades_become_none(raw):
    assert normalize_grade(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10", "10"),
        ("grade_10", "10"),   # cross-file consistency: new leads say "grade_10"
        ("13.0", "13"),
        ("11th", "11"),
        ("12+", "12"),
        ("12_pass", "12"),
        ("12th_pass", "12"),
        ("12th_pass_out", "12"),
        ("1", "1"),
        ("13", "13"),
    ],
)
def test_real_grades_resolve_to_a_bare_integer(raw, expected):
    assert normalize_grade(raw) == expected


def test_grade_stays_consistent_across_the_two_files():
    assert normalize_grade("grade_10") == normalize_grade("10") == "10"


# --- language -------------------------------------------------------------


def test_bare_letter_language_is_expanded():
    assert normalize_language("E") == "English"
    assert normalize_language("Telgu") == normalize_language("Telugu") == "Telugu"
    assert normalize_language("Gujrati") == "Gujarati"
    assert normalize_language("Unknown") is None


# --- LEAD_SOURCE cap ------------------------------------------------------


def _sources_frame(n_sources: int, rows_each: int = 3) -> pd.DataFrame:
    """A history-shaped frame where source popularity is strictly decreasing."""
    rows = []
    for i in range(n_sources):
        for _ in range(rows_each + (n_sources - i)):
            rows.append({"LEAD_SOURCE": f"src-{i:03d}", "REP_ID": "rep-1"})
    return pd.DataFrame(rows)


def test_source_cap_keeps_the_most_frequent_and_buckets_the_tail():
    reference = build_reference_data(_sources_frame(SOURCE_CAP + 5))
    assert len(reference.allowed_sources) == SOURCE_CAP
    assert reference.canonical_source("src-000") == "src-000"
    # src-030..src-034 are the least frequent, so they fall outside the cap.
    for i in range(SOURCE_CAP, SOURCE_CAP + 5):
        assert reference.canonical_source(f"src-{i:03d}") == OTHER_SOURCE


def test_source_cap_merges_case_variants():
    """"International Leads" (522 rows) and "International leads" (5) are one
    source; keying case-insensitively stops one being capped and the other not."""
    frame = pd.DataFrame(
        {"LEAD_SOURCE": ["International Leads"] * 5 + ["International leads"]}
    )
    reference = build_reference_data(frame)
    assert reference.canonical_source("International leads") == "International Leads"


def test_missing_source_stays_missing_rather_than_other():
    reference = build_reference_data(_sources_frame(SOURCE_CAP + 1))
    assert reference.canonical_source(None) is None
    assert reference.canonical_source("  ") is None


def test_the_same_cap_is_applied_to_both_files():
    history = pd.DataFrame(
        {
            "PROSPECTID": ["l1", "l2"],
            "REP_ID": ["r1", "r1"],
            "REP_NAME": ["A", "A"],
            "LEAD_PREDICTION_CATEGORY": ["H", "M"],
            "LEAD_REGION": ["Southern", "Southern"],
            "LEAD_CITY": ["Chennai", "Chennai"],
            "LEAD_FINAL_PREFERRED_LANGUAGE": ["Hindi", "Hindi"],
            "REP_TARGET_EXAM": ["JEE", "JEE"],
            "LEAD_SOURCE": ["IL Website", "IL Website"],
            "LEAD_GRADE": ["10", "11"],
            "LEAD_TOTAL_ATTEMPTED_CALLS": [1, 2],
            "LEAD_CONVERTED_SALE": [0, 1],
            "LEAD_GENERATED_DATE": ["2026-01-05 10:00:00.000"] * 2,
        }
    )
    reference = build_reference_data(history)
    leads = pd.DataFrame(
        {
            "PROSPECTID": ["l1", "l9"],
            "PRED_CATEGORY_WITH_SALES": ["H", None],
            "LEAD_MX_STATE": ["Tamil Nadu", "Tamil Nadu"],
            "LEAD_CITY": ["Chennai", "Chennai"],
            "LEAD_FINAL_PREFERRED_LANGUAGE": ["Hindi", "Hindi"],
            "LEAD_EXAM": ["JEE", "NEET"],
            # The second source never appears in history.
            "LEAD_SOURCE": ["IL Website", "Chat GPT"],
            "LEAD_GRADE": ["grade_10", "grade_11"],
        }
    )
    hist_out = adapt_history(history, reference=reference)
    lead_out = adapt_new_leads(leads, reference=reference)
    assert set(hist_out["lead_source"]) == {"IL Website"}
    assert set(lead_out["lead_source"]) == {"IL Website", OTHER_SOURCE}
    # And both sides agree on the region for the same city.
    assert set(hist_out["lead_geography"]) == set(lead_out["geography"]) == {"South"}


def test_new_leads_without_a_reference_have_no_geography():
    """Honest failure: ingest marks these invalid rather than the loader
    inventing a territory for them."""
    leads = pd.DataFrame(
        {
            "PROSPECTID": ["l1"],
            "PRED_CATEGORY_WITH_SALES": ["H"],
            "LEAD_MX_STATE": ["Tamil Nadu"],
            "LEAD_CITY": ["Chennai"],
            "LEAD_FINAL_PREFERRED_LANGUAGE": ["Hindi"],
            "LEAD_EXAM": ["JEE"],
            "LEAD_SOURCE": ["IL Website"],
            "LEAD_GRADE": ["grade_10"],
        }
    )
    assert adapt_new_leads(leads)["geography"].isna().all()


# --- demo batch sampling --------------------------------------------------

# Roughly the real file: 452,679 rows for 150,594 leads, ~12% H, 5% null intent.
_MIX = {"H": 0.12, "M": 0.38, "L": 0.31, "EL": 0.14, None: 0.05}


def _leads_frame(n: int = 20_000, duplicate_every: int = 3) -> pd.DataFrame:
    buckets = []
    for bucket, share in _MIX.items():
        buckets.extend([bucket] * int(n * share))
    buckets.extend([None] * (n - len(buckets)))
    frame = pd.DataFrame(
        {
            "PROSPECTID": [f"lead-{i:06d}" for i in range(n)],
            "PRED_CATEGORY_WITH_SALES": buckets,
        }
    )
    # Repeat every Nth lead so the frame has duplicate PROSPECTIDs, like the file.
    return pd.concat([frame, frame.iloc[::duplicate_every]], ignore_index=True)


def test_sampling_dedupes_before_it_samples():
    frame = _leads_frame()
    assert frame["PROSPECTID"].duplicated().any()
    sampled = sample_new_leads(frame, 5_000)
    assert len(sampled) == 5_000
    assert not sampled["PROSPECTID"].duplicated().any()


def test_sampling_preserves_the_intent_mix():
    frame = _leads_frame()
    full = intent_mix(frame.drop_duplicates(subset=["PROSPECTID"])["PRED_CATEGORY_WITH_SALES"])
    sampled = intent_mix(sample_new_leads(frame, 5_000)["PRED_CATEGORY_WITH_SALES"])
    assert set(sampled) == set(full)
    for bucket, share in full.items():
        assert abs(sampled[bucket] - share) < 0.005, bucket


def test_sampling_is_deterministic():
    frame = _leads_frame()
    first = sample_new_leads(frame, 1_000)
    assert first.equals(sample_new_leads(frame, 1_000))
    assert not first.equals(sample_new_leads(frame, 1_000, seed=99))


def test_a_tiny_intent_bucket_is_not_lost():
    """A demo batch that dropped every H lead would show nothing useful."""
    frame = pd.DataFrame(
        {
            "PROSPECTID": [f"lead-{i}" for i in range(1_000)],
            "PRED_CATEGORY_WITH_SALES": ["H"] + ["L"] * 999,
        }
    )
    sampled = sample_new_leads(frame, 10)
    assert len(sampled) == 10
    assert "H" in set(sampled["PRED_CATEGORY_WITH_SALES"])


def test_no_cap_returns_every_distinct_lead():
    frame = _leads_frame(1_000)
    for cap in (0, -1, 10_000):
        assert len(sample_new_leads(frame, cap)) == frame["PROSPECTID"].nunique()
