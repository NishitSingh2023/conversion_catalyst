"""Tests for the training path.

``train_model`` is exercised on a tiny in-memory frame (no DB). The negative
downsampling that bounds training memory does need Postgres, because the sampling
is done in SQL - the whole point is that the full history never reaches pandas.
"""
from __future__ import annotations

import random
import uuid

import pandas as pd
import pytest
from sqlalchemy import text

from shared.features import align_features, build_features, normalize_history_columns
from shared.manager_profiles import derive_profiles
from training.train import read_training_history, train_model

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


# ---------------------------------------------------------------------------
# Negative downsampling (SQL; needs Postgres)
# ---------------------------------------------------------------------------
POSITIVES = 20
NEGATIVES = 500


@pytest.fixture
def imbalanced_history(db):
    """Seed a deliberately imbalanced history and report the whole table's balance.

    The sampling query is global, so assertions are written against the table's
    actual class counts rather than assuming these are the only rows in it.
    """
    from shared.db import write_dataframe

    tag = f"S{uuid.uuid4().hex[:8]}"
    rows = [
        {
            "lead_id": f"{tag}-{i}", "manager_id": f"{tag}-MGR",
            "lead_intent_bucket": "H" if i % 2 else "M",
            "lead_language": "Hindi", "lead_geography": "Delhi",
            "lead_product": "JEE", "lead_source": "organic", "lead_grade": "11",
            "first_response_mins": 12.0, "converted": i < POSITIVES,
            "interaction_date": "2026-08-01",
        }
        for i in range(POSITIVES + NEGATIVES)
    ]
    write_dataframe(pd.DataFrame(rows), "lead_manager_history")

    with db.begin() as conn:
        totals = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE converted) AS pos, "
                "       count(*) FILTER (WHERE NOT converted) AS neg "
                "FROM lead_manager_history"
            )
        ).one()

    yield tag, int(totals.pos), int(totals.neg)

    with db.begin() as conn:
        conn.execute(
            text("DELETE FROM lead_manager_history WHERE manager_id = :m"),
            {"m": f"{tag}-MGR"},
        )


def _own(history: pd.DataFrame, tag: str) -> pd.DataFrame:
    return history[history["manager_id"] == f"{tag}-MGR"]


def test_downsampling_keeps_every_positive(db, imbalanced_history):
    """Positives are the entire signal at a 1.2% base rate; none may be dropped."""
    tag, total_pos, total_neg = imbalanced_history
    ratio = 3

    history, sampling = read_training_history(negatives_per_positive=ratio)

    assert sampling["downsampled"] is True
    assert int(history["converted"].sum()) == total_pos, "every positive retained"
    assert int(_own(history, tag)["converted"].sum()) == POSITIVES
    # The negative side is capped, and capped is the only thing it is.
    assert int((~history["converted"]).sum()) == min(ratio * total_pos, total_neg)
    assert int((~history["converted"]).sum()) < total_neg, "negatives were reduced"


def test_downsampling_is_deterministic(db, imbalanced_history):
    """Same data, same sample - so a re-train is reproducible."""
    tag, _, _ = imbalanced_history

    first, _ = read_training_history(negatives_per_positive=3)
    second, _ = read_training_history(negatives_per_positive=3)
    assert list(first["lead_id"]) == list(second["lead_id"])


def test_sampled_history_stays_ordered_by_id(db, imbalanced_history):
    """The train/test split slices positionally, so row order must be stable."""
    tag, _, _ = imbalanced_history

    history, _ = read_training_history(negatives_per_positive=3)
    ids = list(history["id"])
    assert ids == sorted(ids)


def test_sampling_off_reads_everything(db, imbalanced_history):
    """A ratio of 0 disables sampling rather than dropping every negative."""
    tag, total_pos, total_neg = imbalanced_history

    history, sampling = read_training_history(negatives_per_positive=0)

    assert sampling["downsampled"] is False
    assert len(history) == total_pos + total_neg
    assert len(_own(history, tag)) == POSITIVES + NEGATIVES


def test_ratio_wider_than_the_data_is_not_downsampling(db, imbalanced_history):
    """Asking for more negatives than exist reads the full history untouched."""
    tag, total_pos, total_neg = imbalanced_history

    history, sampling = read_training_history(negatives_per_positive=10_000)

    assert sampling["downsampled"] is False
    assert len(history) == total_pos + total_neg
