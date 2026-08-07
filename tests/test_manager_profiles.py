"""Unit tests for manager profile derivation (pure, no DB)."""
from __future__ import annotations

from datetime import date

import pandas as pd

from shared.manager_profiles import derive_profiles


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
