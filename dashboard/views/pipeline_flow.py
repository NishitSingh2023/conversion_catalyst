"""Pipeline Flow view: what the selected run did, end to end (Req 4).

The opening view. It answers one question before any detail: of the leads this
run processed, how many were auto-assigned, how many landed in the claimable
pool, and do those two numbers actually account for every lead. The
reconciliation is the honest part of the story, so it is stated explicitly rather
than left for the reader to add up.
"""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt

# The two reasons the pool stage writes. Listed explicitly so a reason with zero
# rows renders as 0 instead of vanishing from the breakdown.
POOL_REASONS = ("no_eligible_manager", "capacity_overflow")


def render() -> None:
    run_id = st.session_state.get(SELECTED_RUN_KEY)
    st.title("Pipeline Flow")

    if not run_id:
        st.info("No runs are available.")
        return

    with st.spinner("Loading run summary..."):
        header = data.get_run_header(run_id)
        funnel = data.get_funnel_reconciliation(run_id)
        reasons = data.get_pool_reason_breakdown(run_id)

    if header.empty:
        st.info("This run no longer exists in the database.")
        return

    row = header.iloc[0]
    st.caption(f"Run `{run_id}`")

    status_col, stage_col, started_col, completed_col = st.columns(4)
    status_col.metric("Status", fmt.na_or(row["status"]))
    stage_col.metric("Last stage", fmt.na_or(row["stage"]))
    started_col.metric("Started", fmt.format_timestamp(row["started_at"]))
    # Null completed_at means the run is still in flight.
    completed_col.metric("Completed", fmt.format_timestamp(row["completed_at"]))

    st.divider()

    processed = int(row["leads_processed"] or 0)
    assigned = int(row["leads_assigned"] or 0)
    pooled = int(row["leads_pooled"] or 0)

    processed_col, assigned_col, pooled_col = st.columns(3)
    processed_col.metric("Leads processed", f"{processed:,}")
    assigned_col.metric("Leads assigned", f"{assigned:,}")
    assigned_col.caption(
        f"{assigned / processed:.1%} of processed" if processed else "no leads processed"
    )
    pooled_col.metric("Leads pooled", f"{pooled:,}")
    pooled_col.caption(
        f"{pooled / processed:.1%} of processed" if processed else "no leads processed"
    )

    _render_reconciliation(funnel, processed)
    _render_funnel_chart(processed, assigned, pooled)
    _render_pool_reasons(reasons, pooled)
    _render_run_errors(row)


def _render_reconciliation(funnel, processed: int) -> None:
    """State plainly whether assigned + pooled accounts for every lead.

    The counts come from a SQL aggregate over ``assignments`` and ``pool``, not
    from the summary columns on the run row, so this genuinely cross-checks the
    outputs against what the run claimed it did.
    """
    st.subheader("Reconciliation")
    if funnel.empty:
        st.info("No assignment or pool rows for this run.")
        return

    counted = funnel.iloc[0]
    counted_assigned = int(counted["assigned"])
    counted_pooled = int(counted["pooled"])
    reconciled = int(counted["reconciled_total"])

    st.markdown(
        f"Valid leads = assigned + pooled -> "
        f"**{counted_assigned:,}** + **{counted_pooled:,}** = **{reconciled:,}**"
    )

    if reconciled == processed:
        st.success(
            f"Every one of the {processed:,} processed leads is accounted for: "
            f"{counted_assigned:,} assigned, {counted_pooled:,} pooled."
        )
    else:
        difference = processed - reconciled
        st.warning(
            f"{reconciled:,} leads are accounted for against {processed:,} processed, "
            f"a difference of {difference:,}. Leads dropped by validation are counted "
            "as processed but are neither assigned nor pooled."
        )


def _render_funnel_chart(processed: int, assigned: int, pooled: int) -> None:
    """Processed -> assigned funnel, with the pooled remainder alongside."""
    if not processed:
        return
    figure = go.Figure(
        go.Funnel(
            y=["Processed", "Assigned", "Pooled"],
            x=[processed, assigned, pooled],
            textinfo="value+percent initial",
            marker={"color": ["#4c78a8", "#54a24b", "#e45756"]},
        )
    )
    figure.update_layout(margin={"l": 0, "r": 0, "t": 10, "b": 0}, height=280)
    st.plotly_chart(figure, use_container_width=True)


def _render_pool_reasons(reasons, pooled: int) -> None:
    """Pool breakdown by reason, rendering an absent reason as 0."""
    st.subheader("Why leads were pooled")
    counts = dict(zip(reasons["reason"], reasons["n"], strict=False)) if not reasons.empty else {}

    columns = st.columns(len(POOL_REASONS))
    for column, reason in zip(columns, POOL_REASONS, strict=False):
        count = int(counts.get(reason, 0))
        column.metric(reason.replace("_", " ").capitalize(), f"{count:,}")
        column.caption(f"{count / pooled:.1%} of pooled" if pooled else "no pooled leads")

    # Any reason the pool stage writes that this view does not know about.
    unexpected = {k: v for k, v in counts.items() if k not in POOL_REASONS}
    if unexpected:
        st.caption(f"Other reasons recorded: {unexpected}")


def _render_run_errors(row) -> None:
    """Show the run's error text: fatal for a failed run, advisory otherwise."""
    errors = row["errors"]
    if fmt.is_null(errors) or not str(errors).strip():
        return

    if row["status"] == "failed":
        st.subheader("Failure detail")
        st.error(str(errors))
    else:
        # A successful run can still record non-fatal data-quality counts. Worth
        # surfacing, but not as a failure.
        st.subheader("Data-quality notes")
        st.warning(f"The run completed with non-fatal issues recorded: {errors}")
