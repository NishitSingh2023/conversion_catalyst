"""Train the lead-manager conversion model on historical triples.

Pipeline:
    1. Read lead_manager_history from Postgres.
    2. Derive manager profiles from that same history.
    3. Build features (shared.features) for every historical (lead, manager) row
       and use ``converted`` as the target.
    4. Train an XGBoost classifier, handling class imbalance.
    5. Evaluate (AUC / precision / recall) on a held-out split.
    6. Persist the artifact (S3 or local) and register it as the active model.

The pure ``train_model`` function is separated from the DB/S3 side effects so it
can be unit-tested on a tiny in-memory frame.

Usage (local, against docker Postgres):
    DB_PORT=5433 python training/train.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
from sklearn.metrics import precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

# Allow running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.constants import TARGET_COLUMN  # noqa: E402
from shared.features import build_features, normalize_history_columns  # noqa: E402
from shared.manager_profiles import derive_profiles  # noqa: E402
from shared.model_io import (  # noqa: E402
    ModelArtifact,
    artifact_uri,
    new_model_id,
    register_model,
    save_artifact,
)

# ORDER BY is not cosmetic: an unordered SELECT returns rows in heap order, which
# is not stable across reloads (narrower rows pack differently, so page layout -
# and therefore row order - changes). ``train_test_split`` slices positionally,
# so the held-out set silently changed on every reload, moving precision by
# whole points on a test set with only ~15 positives. Ordering by the insertion
# key makes a training run reproducible from the same data.
HISTORY_QUERY = "SELECT * FROM lead_manager_history ORDER BY id"


def _default_n_jobs() -> int:
    """Pick a bounded thread count for XGBoost.

    ``n_jobs=-1`` is deliberately avoided: on high-core machines the
    synchronisation overhead of one thread per core dominates for datasets of
    this size, making training orders of magnitude slower (measured 61s vs 1.8s
    on 19k rows / 14 cores). A modest cap gives the parallelism benefit without
    the oversubscription cost.
    """
    return max(1, min(8, os.cpu_count() or 1))


def train_model(
    history: pd.DataFrame,
    profiles: pd.DataFrame | None = None,
    test_size: float = 0.2,
    random_state: int = 42,
    n_jobs: int | None = None,
) -> tuple[XGBClassifier, list[str], dict]:
    """Train an XGBoost conversion model. Pure function (no DB/S3).

    Returns ``(model, feature_columns, metrics)``.
    """
    n_jobs = _default_n_jobs() if n_jobs is None else n_jobs
    if profiles is None:
        profiles = derive_profiles(history)

    canonical = normalize_history_columns(history)
    X = build_features(canonical, profiles)
    y = history[TARGET_COLUMN].astype(int).to_numpy()
    feature_columns = list(X.columns)

    stratify = y if len(pd.unique(y)) > 1 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=stratify
    )

    # Class imbalance: weight positives by the negative/positive ratio.
    pos = max(int(y_train.sum()), 1)
    neg = max(int(len(y_train) - y_train.sum()), 1)
    scale_pos_weight = neg / pos

    model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.9,
        colsample_bytree=0.9,
        scale_pos_weight=scale_pos_weight,
        eval_metric="auc",
        n_jobs=n_jobs,
        random_state=random_state,
        tree_method="hist",
    )
    model.fit(X_train, y_train)

    proba = model.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)
    metrics = {
        "auc": float(roc_auc_score(y_test, proba)) if len(pd.unique(y_test)) > 1 else None,
        "precision": float(precision_score(y_test, preds, zero_division=0)),
        "recall": float(recall_score(y_test, preds, zero_division=0)),
        "n_features": len(feature_columns),
        "training_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
    }
    return model, feature_columns, metrics


def _resolve_destination(model_id: str) -> str:
    """Local path in dev, S3 URI otherwise."""
    from shared.config import get_config

    cfg = get_config()
    local_dir = os.getenv("MODEL_LOCAL_DIR")
    if cfg.environment == "local" or local_dir:
        base = local_dir or str(Path(__file__).resolve().parents[1] / "models")
        return str(Path(base) / model_id / "model.joblib")
    return artifact_uri(cfg.model_bucket, model_id)


def run() -> dict:
    """Full training run against the configured database."""
    from shared.db import read_sql

    history = read_sql(HISTORY_QUERY)
    if history.empty:
        raise RuntimeError("lead_manager_history is empty; load data first.")

    profiles = derive_profiles(history)
    model, feature_columns, metrics = train_model(history, profiles)

    model_id = new_model_id()
    destination = _resolve_destination(model_id)
    artifact = ModelArtifact(
        model=model,
        feature_columns=feature_columns,
        model_id=model_id,
        metadata=metrics,
    )
    saved_to = save_artifact(artifact, destination)

    register_model(
        model_id=model_id,
        s3_path=saved_to,
        metrics=metrics,
        feature_list=feature_columns,
        training_rows=metrics["training_rows"],
        activate=True,
    )

    result = {"model_id": model_id, "artifact": saved_to, **metrics}
    print(
        f"Trained {model_id} | AUC={metrics['auc']} "
        f"precision={metrics['precision']:.3f} recall={metrics['recall']:.3f} "
        f"| {metrics['n_features']} features | artifact={saved_to}"
    )
    return result


if __name__ == "__main__":
    run()
