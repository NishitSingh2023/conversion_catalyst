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

Shape of the real load (and why it is two passes over history)
-------------------------------------------------------------
The history file is 1.29 GB / 2,719,558 rows, so it is never held in memory:

  pass 1  reads six columns and builds a ``ReferenceData`` - the city->region and
          state->region lookups, the top-30 LEAD_SOURCE cap, and the full REP_ID
          set the Agent-NNNN labels are allocated from.
  pass 2  streams the file in chunks, adapts each chunk with that reference, and
          COPYs it straight into Postgres.

The reference has to come first because **both** sides of the load share it:
history's geography and new_leads' geography are resolved through the same city
lookup, so the model cannot be trained on one vocabulary and served another.
That makes the order in :func:`main` load-bearing - history is scanned before new
leads are adapted, and the reference is threaded from one to the other.

New leads are deduplicated on PROSPECTID and then sampled down to
``--max-new-leads`` (default 30,000), stratified on intent so the batch keeps the
file's H/M/L/EL mix. Eligibility is a leads x 953-managers cross join; the full
150,594 distinct leads is 143M pairs, which is not a demo.

Real datasets are de-identified on the way in (rep names become ``Agent-NNNN``
labels, CRM ids become salted hashes) because the demo account must not hold
personal data. Set ``ANONYMIZE_REAL_DATA=0`` for a production load that needs the
real ProspectID for the LSQ write-back - see ``data.adapt_real_data``.

Usage:
    python scripts/load_sample_data.py
    python scripts/load_sample_data.py --max-new-leads 20000
    ANONYMIZE_REAL_DATA=0 python scripts/load_sample_data.py   # keep real CRM ids
    python scripts/load_sample_data.py --history-csv path/to/history.csv --leads-csv path/to/leads.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from data.adapt_real_data import (  # noqa: E402
    HISTORY_SOURCE_COLUMNS,
    NEW_LEADS_SOURCE_COLUMNS,
    REFERENCE_SCAN_COLUMNS,
    SAMPLE_SEED,
    ReferenceBuilder,
    ReferenceData,
    adapt_history,
    adapt_new_leads,
    intent_mix,
    is_real_history,
    is_real_new_leads,
    sample_new_leads,
)
from shared.db import copy_dataframes, get_engine, write_dataframe  # noqa: E402

SAMPLE_DIR = REPO_ROOT / "data" / "sample"
DEFAULT_HISTORY_CSV = SAMPLE_DIR / "lead_rep_dataset.csv"
DEFAULT_LEADS_CSV = SAMPLE_DIR / "leads_dataset_HML.csv"

#: Source rows per read_csv chunk. Measured on the real 1.29 GB file: 100k rows
#: peaks at ~465 MB resident, 250k at ~860 MB, and the wall clock is identical
#: (~75s either way - the work is CPU-bound in pandas, not in round trips), so the
#: smaller chunk is free. Memory is the constraint worth optimising because the
#: same code has to run against RDS from a modest box.
HISTORY_CHUNK_ROWS = 100_000

#: Leads per demo batch. The full file is 150,594 distinct leads; eligibility
#: crosses every lead with all 953 managers, so the batch is capped.
DEFAULT_MAX_NEW_LEADS = 30_000

HISTORY_TABLE = "lead_manager_history"
HISTORY_COLUMNS = (
    "lead_id",
    "manager_id",
    "manager_name",
    "lead_intent_bucket",
    "lead_geography",
    "lead_language",
    "lead_product",
    "lead_source",
    "lead_grade",
    "contact_attempts",
    "first_response_mins",
    "converted",
    "interaction_date",
)


def _to_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes", "t"])


def _header_columns(csv_path: Path) -> list[str]:
    """Column names only - enough to detect the format and pick ``usecols``."""
    return list(pd.read_csv(csv_path, nrows=0).columns)


def _usable(wanted: tuple[str, ...], available: list[str]) -> list[str]:
    """Intersect wanted columns with what the file actually has, keeping order."""
    present = set(available)
    return [c for c in wanted if c in present]


def scan_history_reference(csv_path: Path, chunksize: int = HISTORY_CHUNK_ROWS) -> ReferenceData:
    """Pass 1: build the shared lookups from a narrow read of the history file."""
    columns = _usable(REFERENCE_SCAN_COLUMNS, _header_columns(csv_path))
    builder = ReferenceBuilder()
    rows = 0
    for chunk in pd.read_csv(csv_path, usecols=columns, chunksize=chunksize, low_memory=False):
        builder.add(chunk)
        rows += len(chunk)
    reference = builder.build()
    print(
        f"  reference scan: {rows:,} rows -> {len(reference.city_to_region):,} cities, "
        f"{len(reference.state_to_region):,} states, "
        f"{len(reference.allowed_sources)} sources kept, "
        f"{len(reference.manager_ids):,} managers"
    )
    return reference


def _adapted_history_chunks(
    csv_path: Path, reference: ReferenceData, chunksize: int
) -> Iterator[pd.DataFrame]:
    """Pass 2: yield adapted chunks, one at a time, so memory stays bounded."""
    columns = _usable(HISTORY_SOURCE_COLUMNS, _header_columns(csv_path))
    rows = 0
    for chunk in pd.read_csv(csv_path, usecols=columns, chunksize=chunksize, low_memory=False):
        rows += len(chunk)
        adapted = adapt_history(chunk, reference=reference)
        print(f"  adapted {rows:,} source rows -> {len(adapted):,} in this chunk", flush=True)
        yield adapted


def load_history(
    csv_path: Path, chunksize: int = HISTORY_CHUNK_ROWS
) -> tuple[int, ReferenceData]:
    """Load history and return ``(rows_written, reference)``.

    The reference is returned rather than rebuilt by the caller: new_leads must be
    adapted with the *same* lookups, and rebuilding invites them to drift.
    """
    if not is_real_history(_header_columns(csv_path)):
        # Already-canonical CSV: small by construction, so the simple path stays.
        df = pd.read_csv(csv_path, low_memory=False)
        df["converted"] = _to_bool(df["converted"])
        with get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE {HISTORY_TABLE} RESTART IDENTITY"))
        return write_dataframe(df, HISTORY_TABLE), ReferenceData()

    print(f"  detected real dataset format in {csv_path.name}; adapting")
    reference = scan_history_reference(csv_path, chunksize)
    written = copy_dataframes(
        _adapted_history_chunks(csv_path, reference, chunksize),
        HISTORY_TABLE,
        columns=HISTORY_COLUMNS,
        truncate=True,
    )
    return written, reference


def read_distinct_new_leads(
    csv_path: Path, chunksize: int = HISTORY_CHUNK_ROWS
) -> tuple[pd.DataFrame, int]:
    """Read the new-leads file into one row per PROSPECTID. Returns ``(frame, rows)``.

    Chunked and deduplicated as it goes, so the 452,679-row file is never held
    alongside the 150,594 rows that survive. Keeping the *first* occurrence within
    each chunk and then across chunks is the same thing as one global keep-first,
    because chunks are concatenated in file order.
    """
    columns = _usable(NEW_LEADS_SOURCE_COLUMNS, _header_columns(csv_path))
    rows = 0
    kept: list[pd.DataFrame] = []
    for chunk in pd.read_csv(csv_path, usecols=columns, chunksize=chunksize, low_memory=False):
        rows += len(chunk)
        kept.append(chunk.drop_duplicates(subset=["PROSPECTID"], keep="first"))
    frame = pd.concat(kept, ignore_index=True).drop_duplicates(
        subset=["PROSPECTID"], keep="first"
    )
    return frame.reset_index(drop=True), rows


def load_new_leads(
    csv_path: Path,
    reference: ReferenceData | None = None,
    max_new_leads: int = DEFAULT_MAX_NEW_LEADS,
    seed: int = SAMPLE_SEED,
) -> int:
    available = _header_columns(csv_path)
    if is_real_new_leads(available):
        print(f"  detected real dataset format in {csv_path.name}; adapting")
        distinct_leads, total_rows = read_distinct_new_leads(csv_path)
        sampled = sample_new_leads(distinct_leads, max_new_leads, seed=seed)
        print(
            f"  {total_rows:,} rows -> {len(distinct_leads):,} distinct leads -> "
            f"sampled {len(sampled):,} (cap {max_new_leads:,})"
        )
        # The mix is compared over *distinct* leads, because that is the population
        # being sampled. Duplicate rows are not spread evenly across intents (L
        # leads are re-contacted far more), so measuring the sample against all
        # 452k rows would look like drift that is not there.
        print(
            "  intent mix  distinct: "
            f"{intent_mix(distinct_leads['PRED_CATEGORY_WITH_SALES'])}"
        )
        print(f"  intent mix   sampled: {intent_mix(sampled['PRED_CATEGORY_WITH_SALES'])}")
        df = adapt_new_leads(sampled, reference=reference)
        if df["geography"].isna().any():
            missing = int(df["geography"].isna().sum())
            print(
                f"  WARNING: {missing:,} of {len(df):,} leads ({missing / len(df):.1%}) "
                "have no resolvable region - almost all of them carry no location at "
                "all in the source (LEAD_CITY 'Unknown' and LEAD_MX_STATE null). "
                "Ingest will mark them invalid rather than inventing a territory."
            )
    else:
        df = pd.read_csv(csv_path, low_memory=False)

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
    parser.add_argument(
        "--max-new-leads",
        type=int,
        default=DEFAULT_MAX_NEW_LEADS,
        help="cap on the demo batch after deduplication; 0 or less means no cap "
        f"(default: {DEFAULT_MAX_NEW_LEADS:,})",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=SAMPLE_SEED,
        help="seed for the stratified new-leads sample (default: %(default)s)",
    )
    parser.add_argument(
        "--history-chunk-rows",
        type=int,
        default=HISTORY_CHUNK_ROWS,
        help="source rows per read/COPY chunk (default: %(default)s)",
    )
    args = parser.parse_args()

    started = time.monotonic()
    n_hist, reference = load_history(Path(args.history_csv), args.history_chunk_rows)
    print(f"loaded {n_hist:,} rows into lead_manager_history in {time.monotonic()-started:.1f}s")

    n_leads = load_new_leads(
        Path(args.leads_csv),
        reference=reference,
        max_new_leads=args.max_new_leads,
        seed=args.sample_seed,
    )
    print(f"loaded {n_leads:,} rows into new_leads")
    print(f"total wall clock {time.monotonic()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
