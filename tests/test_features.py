"""Unit tests for the shared feature-engineering module."""
from __future__ import annotations

import pandas as pd

from shared.features import (
    align_features,
    build_features,
    normalize_history_columns,
)
from shared.manager_profiles import derive_profiles


def _profiles() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "manager_id": "MGR1",
                "languages_handled": ["Hindi", "English"],
                "geographies_handled": ["Delhi"],
                "products_handled": ["JEE"],
                "conv_rate_overall": 0.5, "conv_rate_H": 0.6, "conv_rate_M": 0.3,
                "conv_rate_L": 0.1, "avg_response_mins": 12.0, "total_leads_handled": 40,
            },
        ]
    )


def _pairs() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Perfect match on language/geography/product.
            {"lead_id": "L1", "manager_id": "MGR1", "intent_bucket": "H",
             "geography": "Delhi", "language": "Hindi", "product_interest": "JEE",
             "lead_source": "organic", "grade": "11"},
            # Mismatch on all three.
            {"lead_id": "L2", "manager_id": "MGR1", "intent_bucket": "L",
             "geography": "Chennai", "language": "Tamil", "product_interest": "NEET",
             "lead_source": "paid_search", "grade": "9"},
        ]
    )


def test_match_features_reflect_overlap():
    feats = build_features(_pairs(), _profiles())
    # Row 0 matches everything, row 1 matches nothing.
    assert feats.loc[0, "language_match"] == 1.0
    assert feats.loc[0, "geography_match"] == 1.0
    assert feats.loc[0, "product_overlap"] == 1.0
    assert feats.loc[1, "language_match"] == 0.0
    assert feats.loc[1, "geography_match"] == 0.0
    assert feats.loc[1, "product_overlap"] == 0.0


def test_manager_numeric_features_joined():
    feats = build_features(_pairs(), _profiles())
    assert feats.loc[0, "conv_rate_overall"] == 0.5
    assert feats.loc[0, "avg_response_mins"] == 12.0


def test_features_are_all_numeric():
    feats = build_features(_pairs(), _profiles())
    assert all(str(dt) == "float64" for dt in feats.dtypes)


def test_align_features_adds_and_drops_columns():
    feats = build_features(_pairs(), _profiles())
    trained_cols = list(feats.columns) + ["intent_bucket_EL"]  # a col unseen now
    aligned = align_features(feats, trained_cols)
    assert list(aligned.columns) == trained_cols
    assert (aligned["intent_bucket_EL"] == 0.0).all()


def test_train_serve_consistency_on_same_row():
    """History (renamed) and new-lead representations of the same lead must
    produce identical features - the core train/serve-skew guarantee."""
    profiles = _profiles()

    new_lead = pd.DataFrame(
        [{"lead_id": "L1", "manager_id": "MGR1", "intent_bucket": "H",
          "geography": "Delhi", "language": "Hindi", "product_interest": "JEE",
          "lead_source": "organic", "grade": "11"}]
    )
    history_row = pd.DataFrame(
        [{"lead_id": "L1", "manager_id": "MGR1", "lead_intent_bucket": "H",
          "lead_geography": "Delhi", "lead_language": "Hindi", "lead_product": "JEE",
          "lead_source": "organic", "lead_grade": "11"}]
    )
    f_new = build_features(new_lead, profiles)
    f_hist = build_features(normalize_history_columns(history_row), profiles)
    pd.testing.assert_frame_equal(f_new, f_hist)


def test_profiles_to_features_end_to_end():
    """derive_profiles output plugs straight into build_features."""
    history = pd.DataFrame(
        [{"manager_id": "MGR1", "lead_intent_bucket": "H", "lead_language": "Hindi",
          "lead_geography": "Delhi", "lead_product": "JEE", "first_response_mins": 10,
          "converted": True, "interaction_date": "2026-08-01"}]
    )
    profiles = derive_profiles(history)
    feats = build_features(_pairs(), profiles)
    assert len(feats) == 2
