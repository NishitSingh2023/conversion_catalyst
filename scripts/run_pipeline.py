#!/usr/bin/env python
"""Run the full assignment pipeline locally, stage by stage.

This is the local stand-in for the Step Functions state machine: it invokes each
stage's ``lambda_handler`` in order, threading the run context (run_id, batch_id,
business_date, ...) from one stage to the next exactly as the state machine does.
Useful for demos and end-to-end verification against the docker Postgres.

Usage:
    DB_PORT=5433 python scripts/run_pipeline.py
    DB_PORT=5433 python scripts/run_pipeline.py --batch-id real-2026-08-08
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lambdas.eligibility.handler import lambda_handler as eligibility  # noqa: E402
from lambdas.ingest.handler import lambda_handler as ingest  # noqa: E402
from lambdas.lsq_push.handler import lambda_handler as lsq_push  # noqa: E402
from lambdas.optimizer.handler import lambda_handler as optimize  # noqa: E402
from lambdas.pool.handler import lambda_handler as pool  # noqa: E402
from lambdas.scoring.handler import lambda_handler as scoring  # noqa: E402

STAGES = [
    ("ingest", ingest),
    ("eligibility", eligibility),
    ("scoring", scoring),
    ("optimize", optimize),
    ("pool", pool),
    ("lsq_push", lsq_push),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=None, help="batch to process (default: newest)")
    parser.add_argument("--business-date", default=None, help="capacity window date (YYYY-MM-DD)")
    args = parser.parse_args()

    event: dict = {}
    if args.batch_id:
        event["batch_id"] = args.batch_id
    if args.business_date:
        event["business_date"] = args.business_date

    for name, handler in STAGES:
        print(f"\n=== {name} ===")
        event = handler(event)
        print(json.dumps(event, indent=2, default=str))

    print("\nPipeline complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
