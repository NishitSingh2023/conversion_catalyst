"""Unit test for the pure training function on a tiny in-memory dataset."""
from __future__ import annotations

import random

import pandas as pd

from shared.features import align_features, build_features, normalize_history_columns
from shared.manager_profiles import derive_profiles
from training.train import train_model

LANGS = ["Hindi", "English"]
GEOS = ["Delhi", "Mumbai"]
PRODS = ["JEE", "NEET"]


def _tiny_history(n: int = 200, seed: int = 0) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        mgr = f"MGR{i % 4}"
        lang = rng.choice(LANGS)
        geo = rng.choice(GEOS)
        prod = rng.choice(PRODS)
        bucket = rng.choice(["H", "M", "L", "EL"])
        # Simple learnable rule: H + Hindi converts more often.
        base = {"H": 0.6, "M": 0.35, "L": 0.15, "EL": 0.05}[bucket]
        if lang == "Hindi":
            base += 0.15
        converted = rng.random() < base
        rows.append(
            {
                "lead_id": f"L{i}", "manager_id": mgr, "lead_intent_bucket": bucket,
                "lead_language": lang, "lead_geography": geo, "lead_product": prod,
                "lead_source": "organic", "lead_grade": "10",
                "first_response_mins": rng.uniform(5, 60), "converted": converted,
                "interaction_date": "2026-08-01",
            }
        )
    return pd.DataFrame(rows)


def test_train_model_returns_model_and_metrics():
    history = _tiny_history()
    model, feature_columns, metrics = train_model(history, test_size=0.25)

    assert feature_columns, "expected non-empty feature column list"
    assert {"auc", "precision", "recall", "training_rows"}.issubset(metrics)
    # A learnable rule exists, so AUC should beat random.
    assert metrics["auc"] is None or metrics["auc"] >= 0.5


def test_trained_model_scores_align_with_feature_columns():
    """The trained feature columns must be usable to align an inference frame."""
    history = _tiny_history()
    profiles = derive_profiles(history)
    model, feature_columns, _ = train_model(history, profiles, test_size=0.25)

    # Build an inference pair and align to the trained columns.
    pair = pd.DataFrame(
        [{"lead_id": "X", "manager_id": "MGR0", "intent_bucket": "H",
          "geography": "Delhi", "language": "Hindi", "product_interest": "JEE",
          "lead_source": "organic", "grade": "10"}]
    )
    feats = align_features(build_features(pair, profiles), feature_columns)
    proba = model.predict_proba(feats)[:, 1]
    assert 0.0 <= float(proba[0]) <= 1.0


def test_normalize_history_columns_are_canonical():
    history = _tiny_history(n=4)
    canonical = normalize_history_columns(history)
    for col in ("intent_bucket", "geography", "language", "product_interest"):
        assert col in canonical.columns
