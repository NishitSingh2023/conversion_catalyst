"""Scoring stage: predict conversion probability for each eligible pair.

Reads the eligible (lead, manager) pairs produced by the eligibility stage,
loads the active model from the registry, builds features with the same
shared/ code the model was trained on, and writes one probability per pair to
the ``scores`` table.

Two design choices matter here:

* **Feature parity.** Features come from ``shared.features.build_features`` -
  the exact function training uses - then ``align_features`` reindexes them onto
  the model's stored ``feature_columns``. A category unseen at training becomes
  an all-zero block rather than a shape mismatch, and the count of such rows is
  logged so silent drift is visible.
* **Batched scoring.** At 600 managers the eligible set is hundreds of thousands
  of rows. Pairs are pulled and scored in chunks so peak memory stays flat
  regardless of batch size, mirroring the bounded-write approach used elsewhere.
"""
from __future__ import annotations

import logging
import os

import pandas as pd
from sqlalchemy import text

from shared.constants import MANAGER_NUMERIC_FEATURES
from shared.db import get_engine, read_sql, write_dataframe
from shared.features import align_features, build_features
from shared.model_io import get_active_model, load_artifact
from shared.pipeline import fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _restore_feature_case(profiles):
    """Undo Postgres's lower-casing of the mixed-case profile columns.

    The ``manager_profiles`` DDL declares ``conv_rate_H``/``_M``/``_L`` unquoted,
    so Postgres stores them lower-cased and ``SELECT *`` returns ``conv_rate_h``
    etc. ``build_features`` (shared with training, which builds profiles in
    memory) expects the exact ``MANAGER_NUMERIC_FEATURES`` spelling, so map the
    lower-cased names back before feature building.
    """
    rename = {
        col.lower(): col
        for col in MANAGER_NUMERIC_FEATURES
        if col.lower() != col and col.lower() in profiles.columns
    }
    return profiles.rename(columns=rename) if rename else profiles

# Eligible pairs scored per chunk. Keeps the feature matrix bounded irrespective
# of how large the eligible set is.
SCORING_CHUNK_SIZE = int(os.getenv("SCORING_CHUNK_SIZE", "20000"))

# Pairs joined to the lead attributes the feature builder needs. Manager
# attributes are joined in-process from the profiles frame.
_ELIGIBLE_PAIRS = """
SELECT e.lead_id, e.manager_id,
       n.intent_bucket, n.geography, n.language,
       n.product_interest, n.lead_source, n.grade
FROM eligibility_matrix e
JOIN new_leads n ON n.lead_id = e.lead_id
WHERE e.run_id = :run_id AND e.eligible
ORDER BY e.lead_id, e.manager_id
LIMIT :limit OFFSET :offset
"""


def score_pairs(
    pairs: pd.DataFrame,
    profiles: pd.DataFrame,
    model,
    feature_columns: list[str],
) -> pd.DataFrame:
    """Return ``lead_id, manager_id, conversion_probability`` for a chunk.

    Pure function - no DB, no model loading - so scoring logic is unit-testable
    with a stub estimator.
    """
    if pairs.empty:
        return pd.DataFrame(columns=["lead_id", "manager_id", "conversion_probability"])

    features = align_features(build_features(pairs, profiles), feature_columns)
    proba = model.predict_proba(features)[:, 1]

    return pd.DataFrame(
        {
            "lead_id": pairs["lead_id"].to_numpy(),
            "manager_id": pairs["manager_id"].to_numpy(),
            "conversion_probability": proba,
        }
    )


def _dropped_category_rate(pairs: pd.DataFrame, profiles: pd.DataFrame,
                           feature_columns: list[str]) -> float:
    """Fraction of rows whose one-hot block is entirely zero after alignment.

    A high rate means inference is seeing categories the model never trained on
    (a new geography, a renamed product), which the model scores blindly. Logged
    as an early-warning signal rather than a failure.
    """
    built = build_features(pairs, profiles)
    aligned = align_features(built, feature_columns)
    onehot_cols = [c for c in feature_columns if any(
        c.startswith(p + "_") for p in
        ("intent_bucket", "geography", "language", "product_interest", "lead_source", "grade")
    )]
    if not onehot_cols:
        return 0.0
    all_zero = (aligned[onehot_cols].sum(axis=1) == 0)
    return float(all_zero.mean())


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("scoring requires run_id in the event")

    try:
        active = get_active_model()
        if not active:
            raise RuntimeError("no active model in model_registry; run training first")

        artifact = load_artifact(active["s3_path"])
        feature_columns = artifact.feature_columns
        profiles = _restore_feature_case(read_sql("SELECT * FROM manager_profiles"))

        # Clear any prior scores for an idempotent re-run.
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM scores WHERE run_id = :run_id"), {"run_id": run_id})

        total_pairs = 0
        offset = 0
        checked_drift = False
        while True:
            pairs = read_sql(
                _ELIGIBLE_PAIRS,
                {"run_id": run_id, "limit": SCORING_CHUNK_SIZE, "offset": offset},
            )
            if pairs.empty:
                break

            # Sample drift once, on the first chunk, to avoid repeated cost.
            if not checked_drift:
                rate = _dropped_category_rate(pairs, profiles, feature_columns)
                if rate > 0:
                    logger.warning(
                        "scoring run=%s unseen-category rate=%.2f%% - inference is "
                        "seeing values absent from training", run_id, rate * 100,
                    )
                checked_drift = True

            scored = score_pairs(pairs, profiles, artifact.model, feature_columns)
            scored.insert(0, "run_id", run_id)
            write_dataframe(scored, "scores")

            total_pairs += len(scored)
            offset += SCORING_CHUNK_SIZE

        update_run(run_id, stage="scoring", model_id=active["model_id"])

        logger.info(
            "scoring run=%s model=%s pairs_scored=%s",
            run_id, active["model_id"], total_pairs,
        )

        return {
            **{k: event[k] for k in ("run_id", "batch_id", "business_date") if k in event},
            "model_id": active["model_id"],
            "pairs_scored": total_pairs,
        }
    except Exception as exc:
        logger.exception("scoring failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="scoring")
        raise
