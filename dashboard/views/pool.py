"""Pool view: the leads that were not auto-assigned, and why (Req 8).

The pool is the engine admitting it did not place a lead, which makes the reason
split the most useful thing on the page. ``no_eligible_manager`` means no agent
matched the lead's language, geography, and product at all;
``capacity_overflow`` means agents did match but were already holding their 50.
The two point at very different fixes, so they are shown separately rather than
as one "unassigned" number.

Rows are ordered by ``priority_rank`` ascending, which is the order a manager
claiming from the pool should work down.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt
from dashboard.views._pagination import page_controls

PAGE_SIZE = 50

REASON_EXPLANATIONS = {
    "no_eligible_manager": (
        "No agent matched the lead's language, geography, and product requirements."
    ),
    "capacity_overflow": (
        f"Agents matched, but every candidate was already holding "
        f"{data.MAX_LEADS_PER_MANAGER} leads for the run."
    ),
}


def render() -> None:
    run_id = st.session_state.get(SELECTED_RUN_KEY)
    st.title("Pool")

    if not run_id:
        st.info("No runs are available.")
        return

    with st.spinner("Loading pool..."):
        total = data.count_pool(run_id)
        reasons = data.get_pool_reason_breakdown(run_id)
        statuses = data.get_pool_status_breakdown(run_id)

    if not total:
        st.info("No pooled leads for this run.")
        header = data.get_run_header(run_id)
        if not header.empty and str(header.iloc[0]["status"]) == "running":
            st.caption(
                f"This run is still in progress (current stage: "
                f"`{fmt.na_or(header.iloc[0]['stage'])}`). The pool is populated "
                "after the optimizer stage finishes."
            )
        else:
            st.caption("Every valid lead in this run was auto-assigned.")
        return

    st.caption(f"Run `{run_id}` - {total:,} claimable leads")

    _render_breakdowns(reasons, statuses, total)
    st.divider()
    _render_rows(run_id, total)


def _render_breakdowns(
    reasons: pd.DataFrame, statuses: pd.DataFrame, total: int
) -> None:
    """Reason and status splits, both counted in SQL."""
    reason_col, status_col = st.columns(2)

    with reason_col:
        st.subheader("By reason")
        if reasons.empty:
            st.info("No reasons recorded.")
        else:
            for _, row in reasons.iterrows():
                reason = str(row["reason"])
                count = int(row["n"])
                st.metric(
                    reason.replace("_", " ").capitalize(),
                    f"{count:,}",
                    help=REASON_EXPLANATIONS.get(reason),
                )
                st.caption(f"{count / total:.1%} of the pool")

    with status_col:
        st.subheader("By status")
        if statuses.empty:
            st.info("No statuses recorded.")
        else:
            figure = px.pie(
                statuses,
                names="status",
                values="n",
                hole=0.55,
                color_discrete_sequence=["#4c78a8", "#54a24b"],
            )
            figure.update_layout(margin={"l": 0, "r": 0, "t": 10, "b": 0}, height=260)
            st.plotly_chart(figure, use_container_width=True)
            claimed = int(statuses.loc[statuses["status"] == "claimed", "n"].sum())
            st.caption(f"{claimed:,} of {total:,} pooled leads have been claimed.")


def _render_rows(run_id: str, total: int) -> None:
    """One bounded page of pool entries, highest priority first."""
    st.subheader("Pool entries")
    st.caption("Ordered by priority rank: rank 1 is the highest-priority claimable lead.")

    limit, offset = page_controls(total, PAGE_SIZE, key=f"pool_{run_id}")

    with st.spinner("Loading page..."):
        rows = data.get_pool(run_id, limit, offset)

    if rows.empty:
        st.info("No pool entries on this page.")
        return

    st.dataframe(_to_display_frame(rows), use_container_width=True, hide_index=True)
    st.caption(
        "A null best score means the lead had no eligible agent to score against, so "
        "it reads N/A rather than 0."
    )


def _to_display_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Format a page of pool entries, nulls included."""
    return pd.DataFrame(
        {
            "Rank": rows["priority_rank"],
            "lead_id": rows["lead_id"],
            "Intent": rows["intent_bucket"].map(fmt.na_or),
            # Null for a no_eligible_manager lead: there was nothing to score.
            "Best score": rows["best_score"].map(fmt.format_probability),
            "Reason": rows["reason"].map(fmt.na_or),
            "Status": rows["status"].map(fmt.na_or),
            "Claimed by": rows["claimed_by"].map(fmt.na_or),
            "Claimed at": rows["claimed_at"].map(fmt.format_timestamp),
        }
    )
