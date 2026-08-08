"""Manager Profiles view: the derived agent capabilities behind eligibility (Req 6).

Every agent here is identified by ``manager_id`` alone. ``manager_profiles`` does
carry a ``manager_name`` column for the real dataset, but no query in
:mod:`dashboard.data` selects it, so no personal name can reach this table even
by accident.

The capacity columns are run-scoped: they show what the *selected run* assigned to
each agent and how much of the 50-lead cap that leaves, which is the number that
explains why a strong agent stopped receiving leads mid-run.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt

ARRAY_COLUMNS = ("languages_handled", "geographies_handled", "products_handled")
RATE_COLUMNS = ("conv_rate_overall", "conv_rate_H", "conv_rate_M", "conv_rate_L")

# Column order as displayed. Mirrors Req 6.1 and deliberately excludes any name.
DISPLAY_COLUMNS = (
    "manager_id",
    "current_run_assignments",
    "remaining_capacity",
    "derived_active_flag",
    "languages_handled",
    "geographies_handled",
    "products_handled",
    "conv_rate_overall",
    "conv_rate_H",
    "conv_rate_M",
    "conv_rate_L",
    "avg_response_mins",
    "total_leads_handled",
    "last_active_date",
)


def render() -> None:
    run_id = st.session_state.get(SELECTED_RUN_KEY)
    st.title("Manager Profiles")

    if not run_id:
        st.info("No runs are available.")
        return

    with st.spinner("Loading manager profiles..."):
        profiles = data.get_manager_profiles()
        capacity = data.get_agent_capacity(run_id, data.MAX_LEADS_PER_MANAGER)

    if profiles.empty:
        st.info("No manager profiles are available.")
        st.caption(
            "Profiles are derived from `lead_manager_history` at the start of each "
            "pipeline run."
        )
        return

    merged = profiles.merge(capacity, on="manager_id", how="left")
    merged["current_run_assignments"] = merged["current_run_assignments"].fillna(0).astype(int)
    merged["remaining_capacity"] = (
        merged["remaining_capacity"].fillna(data.MAX_LEADS_PER_MANAGER).astype(int)
    )

    _render_summary(merged)

    st.divider()
    filtered = _render_filters(merged)

    if filtered.empty:
        st.info("No agents match the current filters.")
        return

    st.dataframe(
        _to_display_frame(filtered),
        use_container_width=True,
        hide_index=True,
        height=520,
    )
    st.caption(
        f"{len(filtered):,} of {len(merged):,} agents shown. Agents are identified by "
        f"`manager_id`; no personal name is read or displayed. Capacity is measured "
        f"against the per-run cap of {data.MAX_LEADS_PER_MANAGER}."
    )


def _render_summary(merged: pd.DataFrame) -> None:
    """Headline counts: how many agents exist, are active, and carried load."""
    total_col, active_col, engaged_col, full_col = st.columns(4)
    total_col.metric("Agents profiled", f"{len(merged):,}")

    active = int(merged["derived_active_flag"].fillna(False).astype(bool).sum())
    active_col.metric("Derived active", f"{active:,}")

    engaged = int((merged["current_run_assignments"] > 0).sum())
    engaged_col.metric("Assigned leads this run", f"{engaged:,}")

    at_cap = int((merged["remaining_capacity"] == 0).sum())
    full_col.metric("At capacity", f"{at_cap:,}")


def _render_filters(merged: pd.DataFrame) -> pd.DataFrame:
    """Search and filter controls; returns the filtered frame."""
    search_col, active_col, engaged_col = st.columns([2, 1, 1])
    search = search_col.text_input(
        "Search manager_id", placeholder="e.g. 6181e5", key="profiles_search"
    )
    active_only = active_col.checkbox("Active only", value=False, key="profiles_active_only")
    engaged_only = engaged_col.checkbox(
        "Assigned this run only", value=False, key="profiles_engaged_only"
    )

    filtered = merged
    if search:
        filtered = filtered[
            filtered["manager_id"].astype(str).str.contains(search, case=False, na=False)
        ]
    if active_only:
        filtered = filtered[filtered["derived_active_flag"].fillna(False).astype(bool)]
    if engaged_only:
        filtered = filtered[filtered["current_run_assignments"] > 0]
    return filtered


def _to_display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Format arrays, rates, and nullable scalars for display.

    Formatting happens here rather than in SQL so the underlying frame stays
    numeric for the summary metrics above.
    """
    display = frame.copy()

    for column in ARRAY_COLUMNS:
        display[column] = display[column].map(fmt.format_text_array)
    for column in RATE_COLUMNS:
        display[column] = display[column].map(fmt.format_percent)

    display["avg_response_mins"] = display["avg_response_mins"].map(
        lambda value: fmt.NA_DISPLAY if fmt.is_null(value) else f"{float(value):.1f}"
    )
    display["last_active_date"] = display["last_active_date"].map(
        lambda value: fmt.NA_DISPLAY if fmt.is_null(value) else str(value)
    )
    display["total_leads_handled"] = display["total_leads_handled"].map(
        lambda value: fmt.NA_DISPLAY if fmt.is_null(value) else f"{int(value):,}"
    )

    return display[list(DISPLAY_COLUMNS)].rename(
        columns={
            "manager_id": "Agent (manager_id)",
            "current_run_assignments": "Assigned (run)",
            "remaining_capacity": "Remaining",
            "derived_active_flag": "Active",
            "languages_handled": "Languages",
            "geographies_handled": "Geographies",
            "products_handled": "Products",
            "conv_rate_overall": "Conv overall",
            "conv_rate_H": "Conv H",
            "conv_rate_M": "Conv M",
            "conv_rate_L": "Conv L",
            "avg_response_mins": "Avg response (min)",
            "total_leads_handled": "Leads handled",
            "last_active_date": "Last active",
        }
    )
