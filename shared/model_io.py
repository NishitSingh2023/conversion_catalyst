"""Model artifact persistence and the model registry.

A trained model is stored as a single ``ModelArtifact`` bundle so the scorer
always gets the estimator *and* the exact feature-column layout together (the
column list is what defends against train/serve skew).

Artifacts live in S3 in deployed environments and on the local filesystem during
development. The ``model_registry`` table records metadata + which artifact is
active; the scorer resolves the active artifact through this module.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import joblib


@dataclass
class ModelArtifact:
    model: Any                       # trained estimator (XGBoost)
    feature_columns: list[str]       # exact training column order
    model_id: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Artifact save / load (S3 or local)
# ---------------------------------------------------------------------------
def _is_s3(path: str) -> bool:
    return path.startswith("s3://")


def save_artifact(artifact: ModelArtifact, destination: str) -> str:
    """Serialize an artifact to ``destination`` (local path or s3:// URI).

    Returns the final location string.
    """
    buffer = io.BytesIO()
    joblib.dump(
        {
            "model": artifact.model,
            "feature_columns": artifact.feature_columns,
            "model_id": artifact.model_id,
            "metadata": artifact.metadata,
        },
        buffer,
    )
    buffer.seek(0)

    if _is_s3(destination):
        import boto3

        parsed = urlparse(destination)
        boto3.client("s3").put_object(
            Bucket=parsed.netloc, Key=parsed.path.lstrip("/"), Body=buffer.getvalue()
        )
    else:
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        with open(destination, "wb") as f:
            f.write(buffer.getvalue())
    return destination


def load_artifact(source: str) -> ModelArtifact:
    """Load an artifact from a local path or s3:// URI."""
    if _is_s3(source):
        import boto3

        parsed = urlparse(source)
        with tempfile.NamedTemporaryFile() as tmp:
            boto3.client("s3").download_fileobj(parsed.netloc, parsed.path.lstrip("/"), tmp)
            tmp.seek(0)
            payload = joblib.load(tmp.name)
    else:
        payload = joblib.load(source)

    return ModelArtifact(
        model=payload["model"],
        feature_columns=payload["feature_columns"],
        model_id=payload["model_id"],
        metadata=payload.get("metadata", {}),
    )


def artifact_uri(bucket: str, model_id: str) -> str:
    """Build the canonical S3 URI for a model id."""
    return f"s3://{bucket}/models/{model_id}/model.joblib"


def new_model_id(prefix: str = "conv") -> str:
    return f"{prefix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"


# ---------------------------------------------------------------------------
# Model registry (DB)
# ---------------------------------------------------------------------------
_REGISTER = """
INSERT INTO model_registry (
    model_id, trained_at, s3_path, auc, precision, recall,
    feature_list, training_rows, is_active
) VALUES (
    :model_id, now(), :s3_path, :auc, :precision, :recall,
    :feature_list, :training_rows, :is_active
)
"""


def register_model(
    model_id: str,
    s3_path: str,
    metrics: dict,
    feature_list: list[str],
    training_rows: int,
    activate: bool = True,
) -> None:
    """Insert a model into the registry, optionally making it the active one."""
    import json

    from sqlalchemy import text

    from shared.db import get_engine

    with get_engine().begin() as conn:
        if activate:
            # Enforce the single-active-model invariant before inserting.
            conn.execute(text("UPDATE model_registry SET is_active = FALSE WHERE is_active"))
        conn.execute(
            text(_REGISTER),
            {
                "model_id": model_id,
                "s3_path": s3_path,
                "auc": metrics.get("auc"),
                "precision": metrics.get("precision"),
                "recall": metrics.get("recall"),
                "feature_list": json.dumps(feature_list),
                "training_rows": training_rows,
                "is_active": activate,
            },
        )


def get_active_model() -> dict | None:
    """Return the active model registry row as a dict, or None."""
    from shared.db import read_sql

    df = read_sql("SELECT * FROM model_registry WHERE is_active LIMIT 1")
    if df.empty:
        return None
    return df.iloc[0].to_dict()
