"""Run History view: compare outcomes across runs (Req 10).

Selecting a run here is identical to selecting it in the sidebar, because both
write the same ``st.session_state`` key and rerun. That is deliberate: there is
one notion of "the run I am looking at", not a per-view one.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt


def render() -> None:
    st.title("Run History")

    with st.spinner("Loading run history..."):
        runs = data.get_runs()

    if runs.empty:
        st.info("No run history is available.")
        return

    current = st.session_state.get(SELECTED_RUN_KEY)
    st.caption(
        f"{len(runs):,} run(s), most recent first. Currently viewing `{fmt.na_or(current)}`."
    )

    st.dataframe(
        _to_display_frame(runs, current),
        use_container_width=True,
        hide_index=True,
    )

    _render_selector(runs, current)


def _render_selector(runs: pd.DataFrame, current) -> None:
    """Switch the Selected_Run from here, then rerun so every view repoints.

    Writing :data:`SELECTED_RUN_KEY` is safe because the sidebar selector is
    deliberately keyless, so this key holds plain state rather than widget state.
    """
    st.divider()
    st.subheader("Switch run")

    run_ids = runs["run_id"].tolist()
    index = run_ids.index(current) if current in run_ids else 0

    picker_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
    chosen = picker_col.selectbox(
        "Run to inspect",
        run_ids,
        index=index,
        format_func=lambda rid: _label(runs, rid),
        key="run_history_pick",
    )
    if button_col.button("View this run", use_container_width=True, type="primary"):
        st.session_state[SELECTED_RUN_KEY] = chosen
        st.rerun()


def _label(runs: pd.DataFrame, run_id: str) -> str:
    """``run_id | started_at | status``, matching the sidebar selector."""
    row = runs.loc[runs["run_id"] == run_id]
    if row.empty:
        return run_id
    row = row.iloc[0]
    return f"{run_id} | {fmt.format_timestamp(row['started_at'])} | {row['status']}"


def _to_display_frame(runs: pd.DataFrame, current) -> pd.DataFrame:
    """Format the run table, marking which row is currently selected."""
    return pd.DataFrame(
        {
            "Viewing": [
                "-> current" if run_id == current else "" for run_id in runs["run_id"]
            ],
            "run_id": runs["run_id"],
            "Started": runs["started_at"].map(fmt.format_timestamp),
            # Null while a run is still in flight.
            "Completed": runs["completed_at"].map(fmt.format_timestamp),
            "Status": runs["status"].map(fmt.na_or),
            "Last stage": runs["stage"].map(fmt.na_or),
            "Processed": runs["leads_processed"].map(_count),
            "Assigned": runs["leads_assigned"].map(_count),
            "Pooled": runs["leads_pooled"].map(_count),
        }
    )


def _count(value) -> str:
    """Thousands-separated count, ``"N/A"`` when null."""
    if fmt.is_null(value):
        return fmt.NA_DISPLAY
    return f"{int(value):,}"
