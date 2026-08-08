"""Unit tests for the real-data adapter's de-identification.

The point of these tests is not the hashing itself but the two properties the
rest of the pipeline depends on: the mapping is deterministic (so every join
survives pseudonymisation) and nothing feature-bearing changes when it is
applied.
"""
from __future__ import annotations

import pandas as pd
import pytest

from data import adapt_real_data as ard
from data.adapt_real_data import (
    ReferenceData,
    adapt_history,
    adapt_new_leads,
    agent_labels,
    assert_no_pii_columns,
    build_reference_data,
    pseudonymize_id,
)

FEATURE_COLUMNS = [
    "lead_intent_bucket",
    "lead_geography",
    "lead_language",
    "lead_product",
    "lead_source",
    "lead_grade",
    "contact_attempts",
    "converted",
    "interaction_date",
]


def _history_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PROSPECTID": "lead-a", "REP_ID": "rep-1", "REP_NAME": "Asha Menon",
                "LEAD_PREDICTION_CATEGORY": "H", "LEAD_STATE": "KARNATAKA",
                "LEAD_STATE_CODE": "KA_0120", "LEAD_REGION": "Southern_1423",
                "LEAD_CITY": "Bengaluru",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Telgu", "REP_TARGET_EXAM": "JEE Main",
                "LEAD_SOURCE": "organic", "LEAD_GRADE": "grade_11",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 3, "LEAD_CONVERTED_SALE": 1,
                "LEAD_GENERATED_DATE": "2026-01-05",
            },
            {
                "PROSPECTID": "lead-b", "REP_ID": "rep-2", "REP_NAME": "Ravi Kumar",
                "LEAD_PREDICTION_CATEGORY": None, "LEAD_STATE": "karnataka",
                "LEAD_STATE_CODE": "KA", "LEAD_REGION": "Southern",
                "LEAD_CITY": "bengaluru",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Telugu", "REP_TARGET_EXAM": "NEET",
                "LEAD_SOURCE": "paid", "LEAD_GRADE": "11.0",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 1, "LEAD_CONVERTED_SALE": 0,
                "LEAD_GENERATED_DATE": "2026-01-06",
            },
            {
                # Same lead as the first row, handled by a second rep. LEAD_REGION
                # is missing here, so geography falls back to the city lookup.
                "PROSPECTID": "lead-a", "REP_ID": "rep-1", "REP_NAME": "Asha Menon",
                "LEAD_PREDICTION_CATEGORY": "M", "LEAD_STATE": "Delhi",
                "LEAD_STATE_CODE": "DL", "LEAD_REGION": None,
                "LEAD_CITY": "Delhi",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi", "REP_TARGET_EXAM": "JEE",
                "LEAD_SOURCE": "organic", "LEAD_GRADE": "12",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 0, "LEAD_CONVERTED_SALE": 0,
                "LEAD_GENERATED_DATE": "2026-01-07",
            },
            {
                "PROSPECTID": "lead-d", "REP_ID": "rep-2", "REP_NAME": "Ravi Kumar",
                "LEAD_PREDICTION_CATEGORY": "L", "LEAD_STATE": "DELHI",
                "LEAD_STATE_CODE": "DL_0007", "LEAD_REGION": "Northern\xa0\u200b",
                "LEAD_CITY": "Delhi",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi", "REP_TARGET_EXAM": "NEET",
                "LEAD_SOURCE": "paid", "LEAD_GRADE": "10",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 2, "LEAD_CONVERTED_SALE": 0,
                "LEAD_GENERATED_DATE": "2026-01-08",
            },
        ]
    )


def _new_leads_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "PROSPECTID": "lead-a", "PRED_CATEGORY_WITH_SALES": "H",
                "LEAD_MX_STATE": "KARNATAKA", "LEAD_FINAL_PREFERRED_LANGUAGE": "Telgu",
                "LEAD_EXAM": "JEE Advanced", "LEAD_SOURCE": "organic",
                "LEAD_GRADE": "grade_11", "LEAD_NOTES": "spoke to parent",
                "LEAD_CITY": "Bengaluru", "LEAD_GENDER": "F",
            },
            {
                "PROSPECTID": "lead-c", "PRED_CATEGORY_WITH_SALES": None,
                "LEAD_MX_STATE": "Delhi", "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi",
                "LEAD_EXAM": "NEET", "LEAD_SOURCE": "paid", "LEAD_GRADE": "grade_12",
                "LEAD_NOTES": "call back", "LEAD_CITY": "Delhi", "LEAD_GENDER": "M",
            },
        ]
    )


def _reference() -> ReferenceData:
    """The history-derived lookups, exactly as the loader threads them through."""
    return build_reference_data(_history_df())


def test_history_output_has_no_real_names_or_ids():
    out = adapt_history(_history_df(), anonymize=True)
    assert "Asha Menon" not in set(out["manager_name"])
    assert "Ravi Kumar" not in set(out["manager_name"])
    assert set(out["lead_id"]).isdisjoint({"lead-a", "lead-b", "lead-d"})
    assert set(out["manager_id"]).isdisjoint({"rep-1", "rep-2"})
    assert out["manager_name"].str.fullmatch(r"Agent-\d{4}").all()


def test_pseudonyms_are_deterministic_and_stable_across_files():
    hist = adapt_history(_history_df(), anonymize=True)
    leads = adapt_new_leads(_new_leads_df(), anonymize=True, reference=_reference())

    # Same input -> same output on a second call.
    assert adapt_history(_history_df(), anonymize=True).equals(hist)
    # The same lead in both files gets the same pseudonym, so joins hold.
    shared_id = pseudonymize_id("lead-a", "lead")
    assert shared_id in set(hist["lead_id"])
    assert shared_id in set(leads["lead_id"])
    # Same rep on two rows -> one manager_id and one label.
    rep1 = hist.loc[hist["lead_id"] == shared_id]
    assert rep1["manager_id"].nunique() == 1
    assert rep1["manager_name"].nunique() == 1


def test_lead_and_manager_id_spaces_do_not_alias():
    assert pseudonymize_id("x", "lead") != pseudonymize_id("x", "manager")


def test_agent_labels_are_unique_per_manager():
    ids = pd.Series([f"mgr-{i}" for i in range(500)])
    labels = agent_labels(ids)
    assert len(set(labels.values())) == len(ids)


def test_anonymisation_leaves_every_feature_column_untouched():
    df = _history_df()
    anon = adapt_history(df, anonymize=True)
    raw = adapt_history(df, anonymize=False)
    assert anon[FEATURE_COLUMNS].equals(raw[FEATURE_COLUMNS])

    leads_df = _new_leads_df()
    lead_features = ["intent_bucket", "geography", "language", "product_interest",
                     "lead_source", "grade"]
    reference = _reference()
    anon_leads = adapt_new_leads(leads_df, anonymize=True, reference=reference)
    raw_leads = adapt_new_leads(leads_df, anonymize=False, reference=reference)
    assert anon_leads[lead_features].equals(raw_leads[lead_features])


def test_toggle_off_keeps_real_crm_identifiers():
    """Production loads need the real ProspectID for the LSQ write-back."""
    out = adapt_history(_history_df(), anonymize=False)
    assert set(out["lead_id"]) == {"lead-a", "lead-b", "lead-d"}
    assert set(out["manager_id"]) == {"rep-1", "rep-2"}
    assert "Asha Menon" in set(out["manager_name"])


def test_module_toggle_is_the_default(monkeypatch):
    monkeypatch.setattr(ard, "ANONYMIZE", False)
    assert adapt_history(_history_df())["manager_id"].tolist()[0] == "rep-1"
    monkeypatch.setattr(ard, "ANONYMIZE", True)
    assert adapt_history(_history_df())["manager_id"].tolist()[0] != "rep-1"


def test_no_pii_source_columns_survive_adaptation():
    leads = adapt_new_leads(_new_leads_df(), anonymize=True, reference=_reference())
    assert {"LEAD_NOTES", "LEAD_CITY", "LEAD_GENDER"}.isdisjoint(leads.columns)
    # The guard is what stops a future edit reintroducing one.
    with pytest.raises(AssertionError, match="PII columns"):
        assert_no_pii_columns(leads.assign(LEAD_NOTES="oops"))


def test_guard_catches_a_real_name_reaching_an_identifier_column():
    source = _history_df()
    bad = adapt_history(source, anonymize=True).assign(manager_name="Asha Menon")
    with pytest.raises(AssertionError, match="REP_NAME"):
        assert_no_pii_columns(bad, source, anonymize=True)


def test_salt_change_changes_the_mapping(monkeypatch):
    baseline = pseudonymize_id("lead-a", "lead")
    monkeypatch.setenv("ANONYMIZE_SALT", "another-salt")
    assert pseudonymize_id("lead-a", "lead") != baseline


# --- geography: one vocabulary, shared by both sides ----------------------


def test_history_geography_is_a_canonical_region():
    out = adapt_history(_history_df(), anonymize=True)
    # "Southern_1423" and "Northern\xa0\u200b" both reduce; the LEAD_REGION-less
    # row falls back to the city lookup, which puts Delhi in North.
    assert out["lead_geography"].tolist() == ["South", "South", "North", "North"]


def test_city_is_a_lookup_key_only_and_never_reaches_the_output():
    """LEAD_CITY is PII (it locates a lead) and stays an intermediate."""
    reference = _reference()
    assert reference.region_for_city("Bengaluru") == "South"
    for out in (
        adapt_history(_history_df(), anonymize=True, reference=reference),
        adapt_new_leads(_new_leads_df(), anonymize=True, reference=reference),
    ):
        assert "LEAD_CITY" not in out.columns
        geography = out.get("lead_geography", out.get("geography"))
        assert set(geography.dropna()).isdisjoint({"Bengaluru", "Delhi", "BENGALURU"})


def test_both_files_agree_on_the_region_for_the_same_lead():
    """The whole point of sharing one reference: no train/serve skew."""
    reference = _reference()
    hist = adapt_history(_history_df(), anonymize=True, reference=reference)
    leads = adapt_new_leads(_new_leads_df(), anonymize=True, reference=reference)
    shared = pseudonymize_id("lead-a", "lead")
    hist_regions = set(hist.loc[hist["lead_id"] == shared, "lead_geography"])
    lead_region = leads.loc[leads["lead_id"] == shared, "geography"].iloc[0]
    assert lead_region in hist_regions


def test_chunked_adaptation_matches_a_single_pass():
    """The real load adapts 250k-row chunks. Agent labels are allocated by
    probing a shared 4-digit space, so a chunk that saw only some managers must
    still label them the way the whole file does."""
    df = _history_df()
    reference = _reference()
    whole = adapt_history(df, anonymize=True, reference=reference)
    chunked = pd.concat(
        [
            adapt_history(df.iloc[start : start + 2], anonymize=True, reference=reference)
            for start in range(0, len(df), 2)
        ],
        ignore_index=True,
    )
    assert chunked.equals(whole)


def test_reference_carries_every_manager_id():
    assert _reference().manager_ids == ("rep-1", "rep-2")


def test_empty_reference_is_usable():
    """Adapting history without a reference builds one from the frame itself."""
    assert ReferenceData().region_for_city("Bengaluru") is None
    assert adapt_history(_history_df()).equals(
        adapt_history(_history_df(), reference=_reference())
    )
