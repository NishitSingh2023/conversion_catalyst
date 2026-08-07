"""Unit tests for the eligibility filter rules (pure, no DB)."""
from __future__ import annotations

import pandas as pd

from lambdas.eligibility.handler import evaluate_pairs


def _profiles() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Fully capable and active.
            {"manager_id": "ACTIVE", "languages_handled": ["Hindi"],
             "geographies_handled": ["Delhi"], "derived_active_flag": True},
            # Right coverage but stale.
            {"manager_id": "STALE", "languages_handled": ["Hindi"],
             "geographies_handled": ["Delhi"], "derived_active_flag": False},
            # Active but wrong language.
            {"manager_id": "WRONG_LANG", "languages_handled": ["Tamil"],
             "geographies_handled": ["Delhi"], "derived_active_flag": True},
            # Active, right language, wrong geography.
            {"manager_id": "WRONG_GEO", "languages_handled": ["Hindi"],
             "geographies_handled": ["Mumbai"], "derived_active_flag": True},
        ]
    )


def _lead(lead_id: str = "L1", language: str = "Hindi", geography: str = "Delhi") -> pd.DataFrame:
    return pd.DataFrame([{"lead_id": lead_id, "language": language, "geography": geography}])


def _reason_for(pairs: pd.DataFrame, manager_id: str):
    return pairs.loc[pairs["manager_id"] == manager_id, "rejection_reason"].iloc[0]


def _eligible_for(pairs: pd.DataFrame, manager_id: str) -> bool:
    return bool(pairs.loc[pairs["manager_id"] == manager_id, "eligible"].iloc[0])


def test_only_matching_active_manager_is_eligible():
    pairs = evaluate_pairs(_lead(), _profiles())
    assert _eligible_for(pairs, "ACTIVE")
    assert not _eligible_for(pairs, "STALE")
    assert not _eligible_for(pairs, "WRONG_LANG")
    assert not _eligible_for(pairs, "WRONG_GEO")


def test_rejection_reasons_are_specific():
    pairs = evaluate_pairs(_lead(), _profiles())
    assert _reason_for(pairs, "STALE") == "inactive_no_recent_activity"
    assert _reason_for(pairs, "WRONG_LANG") == "language_mismatch"
    assert _reason_for(pairs, "WRONG_GEO") == "geography_mismatch"
    assert _reason_for(pairs, "ACTIVE") is None


def test_capacity_filter_excludes_full_manager():
    pairs = evaluate_pairs(_lead(), _profiles(), loads={"ACTIVE": 50}, max_leads=50)
    assert not _eligible_for(pairs, "ACTIVE")
    assert _reason_for(pairs, "ACTIVE") == "at_capacity"


def test_capacity_filter_allows_manager_below_cap():
    pairs = evaluate_pairs(_lead(), _profiles(), loads={"ACTIVE": 49}, max_leads=50)
    assert _eligible_for(pairs, "ACTIVE")


def test_inactive_takes_precedence_over_capacity():
    """A stale rep reports inactivity, not capacity, so the reason is actionable."""
    pairs = evaluate_pairs(_lead(), _profiles(), loads={"STALE": 99}, max_leads=50)
    assert _reason_for(pairs, "STALE") == "inactive_no_recent_activity"


def test_lead_with_no_eligible_manager_yields_all_rejections():
    pairs = evaluate_pairs(_lead(language="Bengali", geography="Kolkata"), _profiles())
    assert not pairs["eligible"].any()


def test_every_lead_manager_combination_is_evaluated():
    leads = pd.concat([_lead("L1"), _lead("L2"), _lead("L3")], ignore_index=True)
    pairs = evaluate_pairs(leads, _profiles())
    assert len(pairs) == 3 * 4
    assert set(pairs["lead_id"]) == {"L1", "L2", "L3"}


def test_empty_inputs_return_empty_frame():
    assert evaluate_pairs(pd.DataFrame(), _profiles()).empty
    assert evaluate_pairs(_lead(), pd.DataFrame()).empty
