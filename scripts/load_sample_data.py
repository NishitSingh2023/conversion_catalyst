#!/usr/bin/env python
"""Load sample CSVs into Postgres.

Reads a history CSV and a new-leads CSV and loads them into
``lead_manager_history`` and ``new_leads``. Existing rows for the same batch /
the whole history are cleared first so the loader is idempotent for demos.

Two input formats are accepted and auto-detected per file:
  * the real team dataset (``lead_rep_dataset.csv`` / ``leads_dataset_HML.csv``),
    which is mapped and normalised onto the canonical schema via
    ``data.adapt_real_data``; and
  * an already-canonical CSV whose headers match the table columns.

Defaults point at the real dataset filenames.

Real datasets are de-identified on the way in (rep names become ``Agent-NNNN``
labels, CRM ids become salted hashes) because the demo account must not hold
personal data. Set ``ANONYMIZE_REAL_DATA=0`` for a production load that needs the
real ProspectID for the LSQ write-back - see ``data.adapt_real_data``.

Usage:
    python scripts/load_sample_data.py
    ANONYMIZE_REAL_DATA=0 python scripts/load_sample_data.py   # keep real CRM ids
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

from data.adapt_real_data import (  # noqa: E402
    adapt_history,
    adapt_new_leads,
    is_real_history,
    is_real_new_leads,
)
from shared.db import get_engine, write_dataframe  # noqa: E402

SAMPLE_DIR = REPO_ROOT / "data" / "sample"
DEFAULT_HISTORY_CSV = SAMPLE_DIR / "lead_rep_dataset.csv"
DEFAULT_LEADS_CSV = SAMPLE_DIR / "leads_dataset_HML.csv"


def _to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes", "t"])


def load_history(csv_path: Path) -> int:
    df = pd.read_csv(csv_path, low_memory=False)
    if is_real_history(df):
        print(f"  detected real dataset format in {csv_path.name}; adapting")
        df = adapt_history(df)
    else:
        df["converted"] = _to_bool(df["converted"])
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE lead_manager_history RESTART IDENTITY"))
    return write_dataframe(df, "lead_manager_history")


def load_new_leads(csv_path: Path) -> int:
    df = pd.read_csv(csv_path, low_memory=False)
    if is_real_new_leads(df):
        print(f"  detected real dataset format in {csv_path.name}; adapting")
        df = adapt_new_leads(df)
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
    parser.add_argument("--history-csv", default=str(DEFAULT_HISTORY_CSV))
    parser.add_argument("--leads-csv", default=str(DEFAULT_LEADS_CSV))
    args = parser.parse_args()

    n_hist = load_history(Path(args.history_csv))
    print(f"loaded {n_hist:,} rows into lead_manager_history")

    n_leads = load_new_leads(Path(args.leads_csv))
    print(f"loaded {n_leads:,} rows into new_leads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
