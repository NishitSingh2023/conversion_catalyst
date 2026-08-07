#!/usr/bin/env python
"""Load generated sample CSVs into Postgres.

Reads data/sample/lead_manager_history.csv and data/sample/new_leads.csv and
loads them into the corresponding tables. Existing rows for the same batch /
whole history are cleared first so the loader is idempotent for demos.

The same loader accepts the team-provided dataset: just drop CSVs with the same
column headers into data/sample/ (or pass --history-csv / --leads-csv).

Usage:
    python scripts/load_sample_data.py
    python scripts/load_sample_data.py --history-csv path/to/history.csv --leads-csv path/to/leads.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from shared.db import get_engine, write_dataframe  # noqa: E402

SAMPLE_DIR = REPO_ROOT / "data" / "sample"


def _to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes", "t"])


def load_history(csv_path: Path) -> int:
    df = pd.read_csv(csv_path)
    df["converted"] = _to_bool(df["converted"])
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE lead_manager_history RESTART IDENTITY"))
    return write_dataframe(df, "lead_manager_history")


def load_new_leads(csv_path: Path) -> int:
    df = pd.read_csv(csv_path)
    batch_ids = df["batch_id"].dropna().unique().tolist()
    with get_engine().begin() as conn:
        if batch_ids:
            conn.execute(
                text("DELETE FROM new_leads WHERE batch_id = ANY(:b)"),
                {"b": batch_ids},
            )
    return write_dataframe(df, "new_leads")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-csv", default=str(SAMPLE_DIR / "lead_manager_history.csv"))
    parser.add_argument("--leads-csv", default=str(SAMPLE_DIR / "new_leads.csv"))
    args = parser.parse_args()

    n_hist = load_history(Path(args.history_csv))
    print(f"loaded {n_hist:,} rows into lead_manager_history")

    n_leads = load_new_leads(Path(args.leads_csv))
    print(f"loaded {n_leads:,} rows into new_leads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
