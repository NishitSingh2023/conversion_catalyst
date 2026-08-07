"""Tests for the scoring stage.

score_pairs is pure and tested with a stub estimator. The end-to-end path
(active model -> eligible pairs -> scores table) is covered by an integration
test that trains a tiny model and runs the real handler against Postgres.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lambdas.scoring.handler import score_pairs


class _StubModel:
    """Returns a fixed positive-class probability so assertions are exact."""

    def __init__(self, p: float = 0.7):
        self.p = p

    def predict_proba(self, X):
        n = len(X)
        return np.column_stack([np.full(n, 1 - self.p), np.full(n, self.p)])


def _profiles() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"manager_id": "M1", "languages_handled": ["Hindi"],
             "geographies_handled": ["Delhi"], "products_handled": ["JEE"],
             "conv_rate_overall": 0.5, "conv_rate_H": 0.6, "conv_rate_M": 0.3,
             "conv_rate_L": 0.1, "avg_response_mins": 12.0, "total_leads_handled": 40},
        ]
    )


def _pairs(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"lead_id": f"L{i}", "manager_id": "M1", "intent_bucket": "H",
             "geography": "Delhi", "language": "Hindi", "product_interest": "JEE",
             "lead_source": "organic", "grade": "11"}
            for i in range(n)
        ]
    )


def test_score_pairs_returns_probability_per_pair():
    pairs = _pairs(3)
    from shared.features import build_features
    cols = list(build_features(pairs, _profiles()).columns)

    out = score_pairs(pairs, _profiles(), _StubModel(0.7), cols)
    assert list(out.columns) == ["lead_id", "manager_id", "conversion_probability"]
    assert len(out) == 3
    assert (out["conversion_probability"] == 0.7).all()
    assert out["lead_id"].tolist() == ["L0", "L1", "L2"]


def test_score_pairs_aligns_to_trained_columns():
    """A column present at training but absent now must not break scoring."""
    pairs = _pairs(2)
    from shared.features import build_features
    cols = list(build_features(pairs, _profiles()).columns) + ["geography_Mumbai"]

    out = score_pairs(pairs, _profiles(), _StubModel(0.4), cols)
    assert len(out) == 2  # missing column filled, no shape error


def test_score_pairs_empty_input():
    out = score_pairs(pd.DataFrame(), _profiles(), _StubModel(), ["x"])
    assert out.empty
    assert list(out.columns) == ["lead_id", "manager_id", "conversion_probability"]


def test_probabilities_in_unit_interval():
    pairs = _pairs(5)
    from shared.features import build_features
    cols = list(build_features(pairs, _profiles()).columns)
    out = score_pairs(pairs, _profiles(), _StubModel(0.55), cols)
    assert out["conversion_probability"].between(0.0, 1.0).all()
