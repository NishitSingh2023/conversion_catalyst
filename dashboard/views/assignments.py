"""Assignments view: which agent each lead was routed to, and how surely (Req 7).

Two things this view makes visible that a raw table would not. First, the agents
sitting exactly at the 50-lead cap, which is the mechanism that pushes otherwise
assignable leads into the pool. Second, the meaning of the confidence column: it
is primary minus fallback match score, so for a lead with no fallback there is
nothing to subtract and the honest figure to show is the primary match score
itself.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt
from dashboard.views._pagination import page_controls

PAGE_SIZE = 50

# Cap the distribution chart so a run spread across hundreds of agents stays
# readable; the full picture is in the Manager Profiles table.
MAX_CHART_AGENTS = 30


def render() -> None:
    run_id = st.session_state.get(SELECTED_RUN_KEY)
    st.title("Assignments")

    if not run_id:
        st.info("No runs are available.")
        return

    with st.spinner("Loading assignments..."):
        total = data.count_assignments(run_id)
        push_status = data.get_push_status_breakdown(run_id)
        distribution = data.get_agent_assignment_distribution(
            run_id, data.MAX_LEADS_PER_MANAGER
        )

    if not total:
        st.info("No assignments for this run.")
        # An in-flight run has no assignments *yet*, which is a different statement
        # from a finished run that assigned nothing.
        header = data.get_run_header(run_id)
        if not header.empty and str(header.iloc[0]["status"]) == "running":
            st.caption(
                f"This run is still in progress (current stage: "
                f"`{fmt.na_or(header.iloc[0]['stage'])}`). Assignments appear once "
                "the optimizer stage finishes."
            )
        return

    st.caption(f"Run `{run_id}`")

    _render_push_status(push_status, total)
    st.divider()
    _render_distribution(distribution)
    st.divider()
    _render_rows(run_id, total)


def _render_push_status(push_status: pd.DataFrame, total: int) -> None:
    """Counts per LSQ push status, computed in SQL."""
    st.subheader("Push status")
    if push_status.empty:
        st.info("No push status recorded for this run.")
        return

    columns = st.columns(max(len(push_status), 1))
    for column, (_, row) in zip(columns, push_status.iterrows(), strict=False):
        count = int(row["n"])
        column.metric(str(row["push_status"]).capitalize(), f"{count:,}")
        column.caption(f"{count / total:.1%} of assignments" if total else "")


def _render_distribution(distribution: pd.DataFrame) -> None:
    """Per-agent assignment counts, with capped agents called out.

    ``at_cap`` arrives precomputed from SQL, so this only has to colour by it.
    """
    st.subheader("Assignments per agent")
    if distribution.empty:
        st.info("No agent distribution available for this run.")
        return

    capped = distribution[distribution["at_cap"].astype(bool)]

    agents_col, capped_col, max_col = st.columns(3)
    agents_col.metric("Agents used", f"{len(distribution):,}")
    capped_col.metric(
        f"At the {data.MAX_LEADS_PER_MANAGER}-lead cap", f"{len(capped):,}"
    )
    max_col.metric("Busiest agent", f"{int(distribution['assignment_count'].max()):,}")

    if not capped.empty:
        st.warning(
            f"{len(capped):,} agent(s) hit the per-run cap of "
            f"{data.MAX_LEADS_PER_MANAGER}. Leads that would otherwise have gone to "
            "them overflow into the claimable pool."
        )

    chart_frame = distribution.head(MAX_CHART_AGENTS).copy()
    chart_frame["Capacity"] = chart_frame["at_cap"].map(
        {True: f"At cap ({data.MAX_LEADS_PER_MANAGER})", False: "Below cap"}
    )
    figure = px.bar(
        chart_frame,
        x="manager_id",
        y="assignment_count",
        color="Capacity",
        color_discrete_map={
            f"At cap ({data.MAX_LEADS_PER_MANAGER})": "#e45756",
            "Below cap": "#4c78a8",
        },
        labels={"manager_id": "Agent (manager_id)", "assignment_count": "Assignments"},
    )
    figure.update_layout(
        margin={"l": 0, "r": 0, "t": 10, "b": 0}, height=340, xaxis={"categoryorder": "total descending"}
    )
    figure.add_hline(
        y=data.MAX_LEADS_PER_MANAGER,
        line_dash="dash",
        line_color="grey",
        annotation_text=f"cap {data.MAX_LEADS_PER_MANAGER}",
    )
    st.plotly_chart(figure, use_container_width=True)

    if len(distribution) > MAX_CHART_AGENTS:
        st.caption(
            f"Showing the {MAX_CHART_AGENTS} busiest of {len(distribution):,} agents."
        )


def _render_rows(run_id: str, total: int) -> None:
    """One bounded page of assignment rows."""
    st.subheader("Assignment rows")
    limit, offset = page_controls(total, PAGE_SIZE, key=f"assignments_{run_id}")

    with st.spinner("Loading page..."):
        rows = data.get_assignments(run_id, limit, offset)

    if rows.empty:
        st.info("No assignments on this page.")
        return

    st.dataframe(_to_display_frame(rows), use_container_width=True, hide_index=True)
    st.caption(
        "Confidence is the primary minus fallback match score. Where a lead has no "
        "fallback agent there is no runner-up to subtract, so the primary match "
        "score is shown instead and flagged in the basis column."
    )


def _to_display_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Format a page of assignments, resolving the confidence display rule."""
    display = pd.DataFrame(
        {
            "lead_id": rows["lead_id"],
            "Primary agent": rows["primary_manager_id"],
            "Fallback agent": rows["fallback_manager_id"].map(fmt.na_or),
            "Intent": rows["intent_bucket"].map(fmt.na_or),
            "Confidence": [
                fmt.format_confidence(
                    row["confidence_score"], row["match_score"], row["fallback_manager_id"]
                )
                for _, row in rows.iterrows()
            ],
            "Confidence basis": [
                fmt.confidence_label(row["fallback_manager_id"]) for _, row in rows.iterrows()
            ],
            "Match score": rows["match_score"].map(fmt.format_probability),
            "Assigned at": rows["assigned_at"].map(fmt.format_timestamp),
            "Push status": rows["push_status"].map(fmt.na_or),
        }
    )
    return display
