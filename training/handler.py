"""Lambda entrypoint for model training.

Training runs on the same container image as the pipeline stages, so it uses the
identical shared/ feature code - the strongest guarantee that training and
serving cannot diverge.

Container packaging allows up to 10GB of memory and a 15-minute timeout, which
comfortably covers XGBoost over the expected history volume. If the real dataset
outgrows that, this same entrypoint can be lifted onto Fargate or a SageMaker
training job without touching training/train.py.

Triggered weekly by EventBridge, or on demand:

    aws lambda invoke --function-name lead-assignment-dev-train /dev/stdout
"""
from __future__ import annotations

import logging

from training.train import run

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict | None = None, context=None) -> dict:
    result = run()
    logger.info("training complete: %s", result)
    return result
