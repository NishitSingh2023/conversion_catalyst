"""Feature engineering shared by training and inference.

This is the single place where a ``(lead, manager)`` pair becomes a numeric
feature vector. Both the training job and the scoring Lambda call
``build_features`` so the two can never drift apart (no train/serve skew).

The canonical *lead* feature schema is:
    intent_bucket, geography, language, product_interest, lead_source, grade

Because ``lead_manager_history`` uses ``lead_*`` prefixes, use
``normalize_history_columns`` to map a history frame onto this schema before
building features. ``new_leads`` already uses the canonical names.

Feature groups produced:
    * one-hot encoded lead categoricals
    * manager numeric aggregates (conversion rates, response time, volume)
    * match features (language/geography/product overlap between lead & manager)

At inference time call ``align_features`` to reindex the built frame onto the
exact column list the model was trained with (missing columns filled with 0).
"""
from __future__ import annotations

import pandas as pd

from shared.constants import (
    LEAD_CATEGORICAL_FEATURES,
    MANAGER_NUMERIC_FEATURES,
    MATCH_FEATURES,
)

# Mapping from lead_manager_history column names to the canonical lead schema.
_HISTORY_TO_CANONICAL = {
    "lead_intent_bucket": "intent_bucket",
    "lead_geography": "geography",
    "lead_language": "language",
    "lead_product": "product_interest",
    "lead_source": "lead_source",
    "lead_grade": "grade",
}


def normalize_history_columns(history: pd.DataFrame) -> pd.DataFrame:
    """Rename history's lead_* columns to the canonical lead feature names."""
    return history.rename(columns=_HISTORY_TO_CANONICAL)


def _in_list(value, container) -> int:
    """1 if value is contained in a list/array-like manager attribute, else 0."""
    if container is None:
        return 0
    try:
        return int(value in container)
    except TypeError:
        return 0


def _compute_match_features(pairs: pd.DataFrame) -> pd.DataFrame:
    """Compute language/geography/product match flags between lead & manager.

    Expects the manager list-columns (``languages_handled`` etc.) to already be
    joined onto ``pairs``.
    """
    language_match = [
        _in_list(lang, langs)
        for lang, langs in zip(pairs["language"], pairs["languages_handled"], strict=False)
    ]
    geography_match = [
        _in_list(geo, geos)
        for geo, geos in zip(pairs["geography"], pairs["geographies_handled"], strict=False)
    ]
    product_overlap = [
        _in_list(prod, prods)
        for prod, prods in zip(pairs["product_interest"], pairs["products_handled"], strict=False)
    ]
    return pd.DataFrame(
        {
            "language_match": language_match,
            "geography_match": geography_match,
            "product_overlap": product_overlap,
        },
        index=pairs.index,
    )


def build_features(pairs: pd.DataFrame, profiles: pd.DataFrame) -> pd.DataFrame:
    """Turn ``(lead, manager)`` pairs into a numeric feature matrix.

    Parameters
    ----------
    pairs:
        One row per (lead, manager) candidate. Must contain the canonical lead
        columns plus ``manager_id``.
    profiles:
        Manager profiles (output of ``manager_profiles.derive_profiles``), keyed
        by ``manager_id``.

    Returns
    -------
    A purely-numeric DataFrame (categoricals one-hot encoded) aligned to
    ``pairs.index``.
    """
    joined = pairs.merge(profiles, on="manager_id", how="left")
    joined.index = pairs.index

    # --- match features ---
    match = _compute_match_features(joined)

    # --- manager numeric aggregates ---
    manager_numeric = joined[list(MANAGER_NUMERIC_FEATURES)].fillna(0.0)

    # --- one-hot lead categoricals ---
    categoricals = joined[list(LEAD_CATEGORICAL_FEATURES)].astype("string").fillna("unknown")
    dummies = pd.get_dummies(categoricals, prefix=list(LEAD_CATEGORICAL_FEATURES))

    features = pd.concat([dummies, manager_numeric, match], axis=1)
    # Ensure everything is numeric (booleans -> int) for the model.
    return features.astype(float)


def align_features(features: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    """Reindex an inference feature frame to the trained column order.

    Columns present at training but absent now are added as 0; columns unseen at
    training are dropped. This guarantees the model always receives the exact
    feature layout it was trained on.
    """
    return features.reindex(columns=feature_columns, fill_value=0.0).astype(float)


def match_feature_names() -> list[str]:
    return list(MATCH_FEATURES)
