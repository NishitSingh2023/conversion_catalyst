"""Pure display helpers for the dashboard.

Formatting lives here, away from both Streamlit and the database, because these
are the rules judges actually see -- a null metric must read ``"N/A"`` rather
than ``None`` or ``nan``, a probability must render at a fixed precision, a
Postgres ``text[]`` must read as prose, and a lead with no fallback manager must
show its primary match score as the confidence. Keeping them pure means they are
property-testable on generated inputs without a Streamlit runtime or a live DB.

This module performs no I/O of any kind: it imports neither ``streamlit`` nor
``shared.db``, so it can never reach the database, let alone modify it.

Populated by task 2 (formatters and the pagination window helper).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

# --- Display constants ----------------------------------------------------
# One spelling of "no value", used by every formatter here so a null renders
# identically in a metric, a table cell, and a caption.
NA_DISPLAY = "N/A"

# An empty Postgres ``text[]`` is a real, meaningful value (a manager who has
# handled no products yet), not a missing one, so it gets its own placeholder
# rather than collapsing into NA_DISPLAY.
EMPTY_ARRAY_DISPLAY = "(none)"

# Default precisions. Probabilities are read at three decimals because scored
# managers frequently differ in the third place; rates are read as percentages
# at one decimal, which is all the precision a conversion rate deserves.
PROBABILITY_PRECISION = 3
PERCENT_PRECISION = 1

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def is_null(value: Any) -> bool:
    """Return True for SQL/Python nulls (``None``, ``NaN``, ``NaT``).

    Containers are never null: an empty list is an empty list, not a missing
    value. This matters because ``pandas.isna`` on a list returns an *array* of
    element-wise results, which would raise when coerced to a single bool.
    """
    if value is None:
        return True
    if isinstance(value, str | bytes):
        return False
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        return False
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    # Guard against array-likes that slipped past the isinstance checks above.
    if isinstance(result, bool):
        return result
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def na_or(value: Any) -> str:
    """Render any scalar, mapping nulls to ``"N/A"``.

    The generic fallback for values with no dedicated formatter. Non-null values
    are stringified as-is, so this never returns ``"N/A"`` for a real value.
    """
    if is_null(value):
        return NA_DISPLAY
    return str(value)


def format_probability(value: Any, precision: int = PROBABILITY_PRECISION) -> str:
    """Render a 0..1 probability at fixed precision, or ``"N/A"`` if null.

    Fixed-point (not general) formatting, so the output always carries exactly
    ``precision`` decimal places and never falls back to scientific notation for
    the very small probabilities the scorer produces.
    """
    if is_null(value):
        return NA_DISPLAY
    precision = max(0, int(precision))
    return f"{float(value):.{precision}f}"


def format_percent(value: Any, precision: int = PERCENT_PRECISION) -> str:
    """Render a 0..1 *fraction* as a percentage, or ``"N/A"`` if null.

    Input is the fraction as stored (``conv_rate_overall`` etc. are 0..1), so
    0.25 renders as ``"25.0%"``.
    """
    if is_null(value):
        return NA_DISPLAY
    precision = max(0, int(precision))
    return f"{float(value) * 100:.{precision}f}%"


def format_text_array(value: Any) -> str:
    """Render a Postgres ``text[]`` (a Python list) as a readable string.

    Every element of the input appears in the output, joined by commas. The
    empty array maps to :data:`EMPTY_ARRAY_DISPLAY`; a genuinely null column
    maps to :data:`NA_DISPLAY`. Never raises, whatever the element types.
    """
    if is_null(value):
        return NA_DISPLAY
    if isinstance(value, str):
        # Already rendered upstream (or a single scalar), pass through.
        return value or EMPTY_ARRAY_DISPLAY
    if isinstance(value, Iterable):
        items = [str(item) for item in value]
        if not items:
            return EMPTY_ARRAY_DISPLAY
        return ", ".join(items)
    return str(value)


def format_timestamp(value: Any, fmt: str = TIMESTAMP_FORMAT) -> str:
    """Render a timestamp, or ``"N/A"`` for a null (e.g. a still-running run)."""
    if is_null(value):
        return NA_DISPLAY
    try:
        return pd.Timestamp(value).strftime(fmt)
    except (TypeError, ValueError):
        return str(value)


def format_confidence(
    confidence_score: Any,
    match_score: Any,
    fallback_manager_id: Any,
    precision: int = PROBABILITY_PRECISION,
) -> str:
    """Render an assignment's confidence, accounting for the no-fallback case.

    Confidence is defined as primary minus fallback match score, so it is only
    meaningful when a fallback exists. When ``fallback_manager_id`` is null there
    was no runner-up to subtract, and the figure judges should read is the
    primary ``match_score`` itself. The optimizer already stores it that way; this
    reads ``match_score`` directly so the display is correct even if a row was
    written by an older optimizer build.
    """
    if is_null(fallback_manager_id):
        return format_probability(match_score, precision)
    return format_probability(confidence_score, precision)


def confidence_label(fallback_manager_id: Any) -> str:
    """Explain which quantity :func:`format_confidence` just displayed."""
    if is_null(fallback_manager_id):
        return "primary match score (no fallback manager)"
    return "primary minus fallback match score"


# --- Pagination -----------------------------------------------------------
# Kept beside the formatters because it is the other piece of pure, generated-
# input logic in the dashboard: every paged read gets its bounds from here, so
# no view can accidentally issue an unbounded SELECT over a per-pair table.

def page_window(page_index: int, page_size: int) -> tuple[int, int]:
    """Return the ``(limit, offset)`` bind values for a zero-based page index.

    A negative page index clamps to the first page so ``offset`` is never
    negative (Postgres rejects a negative OFFSET). ``page_size`` is floored at 1
    so the returned ``limit`` is always a real bound.
    """
    page_index = max(0, int(page_index))
    page_size = max(1, int(page_size))
    return page_size, page_index * page_size


def page_count(total_rows: Any, page_size: int) -> int:
    """Number of pages needed for ``total_rows``, always at least 1.

    Returning 1 for an empty table keeps the view's prev/next controls in a
    valid state instead of rendering "page 1 of 0".
    """
    if is_null(total_rows):
        return 1
    total_rows = max(0, int(total_rows))
    page_size = max(1, int(page_size))
    return max(1, -(-total_rows // page_size))
