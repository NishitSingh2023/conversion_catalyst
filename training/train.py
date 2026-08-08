"""Train the lead-manager conversion model on historical triples.

Pipeline:
    1. Refresh manager profiles in Postgres and read them back.
    2. Read a class-balanced sample of lead_manager_history.
    3. Build features (shared.features) for every sampled (lead, manager) row
       and use ``converted`` as the target.
    4. Train an XGBoost classifier, handling class imbalance.
    5. Evaluate (AUC / precision / recall) on a held-out split.
    6. Persist the artifact (S3 or local) and register it as the active model.

The pure ``train_model`` function is separated from the DB/S3 side effects so it
can be unit-tested on a tiny in-memory frame.

Memory
------
Reading all 2,719,558 history rows and one-hot encoding them measured **7.9GB
peak RSS** in 76s. The training function has 8192MB, so that run fits in wall
clock and does not fit in memory - it would be killed, and only in production,
where the dataset is largest. The 88-column float64 matrix is ~1.9GB on its own
and ``train_test_split`` copies it, on top of the ~2.3GB history frame.

The fix is negative downsampling: **every positive is kept** and negatives are
sampled to ``TRAIN_NEGATIVES_PER_POSITIVE`` per positive. On a 1.2% base rate the
positives are the entire signal - 32,530 of them - and discarding any would trade
away the thing the model is trying to learn to save memory that the negatives are
wasting. ``scale_pos_weight`` already rescales for whatever ratio results, so the
model's calibration follows the sample rather than being distorted by it.

Usage (local, against docker Postgres):
    DB_PORT=5433 python training/train.py
"""
from __future__ import annotations

import logging
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
from shared.manager_profiles import (  # noqa: E402
    derive_profiles,
    read_manager_profiles,
    refresh_manager_profiles,
)
from shared.model_io import (  # noqa: E402
    ModelArtifact,
    artifact_uri,
    new_model_id,
    register_model,
    save_artifact,
)

logger = logging.getLogger(__name__)

# Negatives retained per positive. All positives are kept regardless; this only
# bounds the majority class. 10:1 keeps ~358k of 2.7M rows, which is a ~1.0GB
# peak instead of 7.9GB, and leaves the negative class far larger than the
# positive one so the decision boundary is still estimated against plenty of
# counterexamples. Set to 0 (or negative) to disable sampling and train on the
# full history - only advisable outside Lambda.
TRAIN_NEGATIVES_PER_POSITIVE = int(os.getenv("TRAIN_NEGATIVES_PER_POSITIVE", "10"))

_COUNT_BY_CLASS = """
SELECT count(*) FILTER (WHERE converted)     AS positives,
       count(*) FILTER (WHERE NOT converted) AS negatives
FROM lead_manager_history
"""

# ORDER BY id is not cosmetic: an unordered SELECT returns rows in heap order,
# which is not stable across reloads (narrower rows pack differently, so page
# layout - and therefore row order - changes). ``train_test_split`` slices
# positionally, so the held-out set silently changed on every reload, moving
# precision by whole points on a test set with only ~15 positives. Ordering by the
# insertion key makes a training run reproducible from the same data.
_FULL_HISTORY_QUERY = "SELECT * FROM lead_manager_history ORDER BY id"

# The sampled read. Two properties to preserve:
#
#   * Every positive survives - the WHERE converted branch is unfiltered.
#   * The negative sample is deterministic. ``ORDER BY md5(id::text)`` is a
#     stable pseudo-random ordering: it depends only on the row's id, so the same
#     data yields the same sample on every run, unlike ``random()`` or heap order.
#     Hashing rather than ``id % n`` matters because ids follow CSV insertion
#     order, which is not independent of the lead attributes.
#
# The final ORDER BY id restores insertion order across the recombined set so the
# positional train/test split stays reproducible.
_SAMPLED_HISTORY_QUERY = """
SELECT * FROM (
    SELECT * FROM lead_manager_history WHERE converted
    UNION ALL
    SELECT * FROM (
        SELECT * FROM lead_manager_history
        WHERE NOT converted
        ORDER BY md5(id::text)
        LIMIT :negative_limit
    ) sampled_negatives
) balanced
ORDER BY id
"""


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
        # Recorded because precision at a fixed 0.5 threshold moves with the test
        # set's class balance, and the caller may have downsampled negatives. On
        # the real data, precision reads 0.07 against the natural 1.2% base rate
        # and 0.37 against a 10:1 sample of it - the same model either way. Two
        # registry rows are only comparable when this rate matches.
        "test_positive_rate": float(y_test.mean()) if len(y_test) else 0.0,
    }
    return model, feature_columns, metrics


def read_training_history(
    negatives_per_positive: int = TRAIN_NEGATIVES_PER_POSITIVE,
) -> tuple[pd.DataFrame, dict]:
    """Read history for training, downsampling negatives in SQL.

    Returns ``(history, sampling)`` where ``sampling`` records what the sample was
    drawn from so the registered model can say what it saw.

    The sampling happens in the query, not in pandas: the point is that the 2.7M
    rows never enter this process.
    """
    from shared.db import read_sql

    counts = read_sql(_COUNT_BY_CLASS).iloc[0]
    positives = int(counts["positives"])
    negatives = int(counts["negatives"])
    sampling = {
        "history_rows_available": positives + negatives,
        "history_positives": positives,
        "negatives_per_positive": negatives_per_positive,
    }

    negative_limit = negatives_per_positive * positives
    if negatives_per_positive <= 0 or negative_limit >= negatives:
        # Nothing to gain from sampling: either it is switched off, or the
        # requested ratio already covers every negative there is.
        sampling["downsampled"] = False
        return read_sql(_FULL_HISTORY_QUERY), sampling

    history = read_sql(_SAMPLED_HISTORY_QUERY, {"negative_limit": negative_limit})
    sampling["downsampled"] = True
    sampling["history_negatives_sampled"] = int(negative_limit)
    logger.info(
        "training sample: kept all %s positives + %s of %s negatives (%s:1) = %s rows",
        positives, negative_limit, negatives, negatives_per_positive, len(history),
    )
    return history, sampling


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
    # Profiles are refreshed and read back rather than derived from the history
    # frame. Two reasons, and both matter more now than they used to:
    #   * The frame below is a *sample*, so deriving profiles from it would
    #     compute every manager's conversion rate and coverage list from ~13% of
    #     their rows. Profiles must come from the whole history.
    #   * The SQL refresh aggregates all 2.7M rows in Postgres for ~130MB, where
    #     the pandas derivation needed 2.3GB to produce the same 953 rows.
    # Reading the materialised table also means the model trains on the exact
    # profile values the scoring stage will feed it.
    refresh_manager_profiles()
    profiles = read_manager_profiles()

    history, sampling = read_training_history()
    if history.empty:
        raise RuntimeError("lead_manager_history is empty; load data first.")

    model, feature_columns, metrics = train_model(history, profiles)
    metrics = {**metrics, **sampling}

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
    sample_note = (
        f"{metrics['history_positives']} positives (all) + "
        f"{metrics['history_negatives_sampled']} negatives sampled from "
        f"{metrics['history_rows_available']}"
        if metrics["downsampled"]
        else f"{metrics['history_rows_available']} rows (no downsampling)"
    )
    print(
        f"Trained {model_id} | AUC={metrics['auc']} "
        f"precision={metrics['precision']:.3f} recall={metrics['recall']:.3f} "
        f"| {metrics['n_features']} features | {sample_note} | artifact={saved_to}"
    )
    return result


if __name__ == "__main__":
    run()
