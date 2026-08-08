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
    adapt_history,
    adapt_new_leads,
    agent_labels,
    assert_no_pii_columns,
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
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Telgu", "REP_TARGET_EXAM": "JEE Main",
                "LEAD_SOURCE": "organic", "LEAD_GRADE": "grade_11",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 3, "LEAD_CONVERTED_SALE": 1,
                "LEAD_GENERATED_DATE": "2026-01-05",
            },
            {
                "PROSPECTID": "lead-b", "REP_ID": "rep-2", "REP_NAME": "Ravi Kumar",
                "LEAD_PREDICTION_CATEGORY": None, "LEAD_STATE": "karnataka",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Telugu", "REP_TARGET_EXAM": "NEET",
                "LEAD_SOURCE": "paid", "LEAD_GRADE": "11.0",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 1, "LEAD_CONVERTED_SALE": 0,
                "LEAD_GENERATED_DATE": "2026-01-06",
            },
            {
                # Same lead as the first row, handled by a second rep.
                "PROSPECTID": "lead-a", "REP_ID": "rep-1", "REP_NAME": "Asha Menon",
                "LEAD_PREDICTION_CATEGORY": "M", "LEAD_STATE": "Delhi",
                "LEAD_FINAL_PREFERRED_LANGUAGE": "Hindi", "REP_TARGET_EXAM": "JEE",
                "LEAD_SOURCE": "organic", "LEAD_GRADE": "12",
                "LEAD_TOTAL_ATTEMPTED_CALLS": 0, "LEAD_CONVERTED_SALE": 0,
                "LEAD_GENERATED_DATE": "2026-01-07",
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


def test_history_output_has_no_real_names_or_ids():
    out = adapt_history(_history_df(), anonymize=True)
    assert "Asha Menon" not in set(out["manager_name"])
    assert "Ravi Kumar" not in set(out["manager_name"])
    assert set(out["lead_id"]).isdisjoint({"lead-a", "lead-b"})
    assert set(out["manager_id"]).isdisjoint({"rep-1", "rep-2"})
    assert out["manager_name"].str.fullmatch(r"Agent-\d{4}").all()


def test_pseudonyms_are_deterministic_and_stable_across_files():
    hist = adapt_history(_history_df(), anonymize=True)
    leads = adapt_new_leads(_new_leads_df(), anonymize=True)

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
    anon_leads = adapt_new_leads(leads_df, anonymize=True)
    raw_leads = adapt_new_leads(leads_df, anonymize=False)
    assert anon_leads[lead_features].equals(raw_leads[lead_features])


def test_toggle_off_keeps_real_crm_identifiers():
    """Production loads need the real ProspectID for the LSQ write-back."""
    out = adapt_history(_history_df(), anonymize=False)
    assert set(out["lead_id"]) == {"lead-a", "lead-b"}
    assert set(out["manager_id"]) == {"rep-1", "rep-2"}
    assert "Asha Menon" in set(out["manager_name"])


def test_module_toggle_is_the_default(monkeypatch):
    monkeypatch.setattr(ard, "ANONYMIZE", False)
    assert adapt_history(_history_df())["manager_id"].tolist()[0] == "rep-1"
    monkeypatch.setattr(ard, "ANONYMIZE", True)
    assert adapt_history(_history_df())["manager_id"].tolist()[0] != "rep-1"


def test_no_pii_source_columns_survive_adaptation():
    leads = adapt_new_leads(_new_leads_df(), anonymize=True)
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
