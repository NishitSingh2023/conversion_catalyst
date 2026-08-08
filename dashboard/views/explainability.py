"""Explainability view: why one specific lead ended up where it did (Req 9).

Scores alone do not explain an outcome. The optimizer walks a lead's candidates in
descending probability order and takes the first one with a seat left, so the
interesting question is usually not "who scored highest" but "why didn't the
highest scorer get it". This view answers that directly: it shows the ranked
candidate list, marks the Agent that was actually chosen, and shows each
candidate's load against the cap, so a lead that went to its fourth-best Agent
visibly shows the three better ones sitting full.

Capacity is reported on the **business date**, not per run. That is the window the
optimizer enforces the cap over (via the ``manager_daily_load`` view, which sums
assignments plus pool claims across every run sharing a date), and using a
per-run count would understate an Agent's real load whenever two runs share a
date, producing a confident but wrong explanation.

The other thing this view is careful about: the eligibility stage samples which
leads get their rejection rows persisted, so an empty rejection list is ambiguous.
The presence check distinguishes "no Agent was rejected" from "we did not record
it", and the view never claims the former when it only knows the latter.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import SELECTED_RUN_KEY, data
from dashboard import format as fmt
from dashboard.views._pagination import page_controls

CANDIDATE_PAGE_SIZE = 25
ELIGIBLE_PAGE_SIZE = 50
REJECTION_PAGE_SIZE = 25

# Canonical session key for the chosen lead. Not a widget key, so the quick-pick
# buttons can write it and the picker will follow on the next rerun.
LEAD_KEY = "_explain_lead_id"

SAMPLING_CAVEAT = (
    "Rejection reasons are captured for a sampled subset of leads. Every eligible "
    "pair is persisted, but rejection rows are only written for a limited number "
    "of leads per run, so rejection detail is partial by design."
)


def render() -> None:
    run_id = st.session_state.get(SELECTED_RUN_KEY)
    st.title("Explainability")

    if not run_id:
        st.info("No runs are available.")
        return

    st.caption(
        f"Run `{run_id}` - trace a single lead from its attributes through to the "
        "assignment or pool decision."
    )

    with st.spinner("Loading leads..."):
        lead_ids = data.get_run_lead_ids(run_id)
        interesting = data.get_default_interesting_leads(run_id)

    if lead_ids.empty:
        _render_no_decisions(run_id)
        return

    available = lead_ids["lead_id"].tolist()
    _render_quick_picks(interesting, available)
    lead_id = _render_lead_picker(available)

    if not lead_id:
        st.info("Select a lead to trace.")
        return

    with st.spinner("Loading lead trace..."):
        attributes = data.get_lead_attributes(lead_id)
        decision = data.get_lead_decision(run_id, lead_id)
        pool_entry = data.get_lead_pool_entry(run_id, lead_id)
        context = data.get_decision_context(run_id, lead_id, data.MAX_LEADS_PER_MANAGER)
        counts = data.count_lead_pairs(run_id, lead_id)
        has_rejections = data.has_rejection_rows(run_id, lead_id)

    # A lead is only genuinely unknown if nothing at all references it. Attributes
    # can disappear on their own: `new_leads` holds the *current* batch, so a later
    # ingest replaces it, while this run's assignments, scores, and pool rows
    # survive. In that case the decision is still fully explainable and bailing out
    # would throw away the part that matters.
    if attributes.empty and decision.empty and pool_entry.empty:
        st.warning("Lead not found in this run's batch.")
        return

    eligible_count, rejected_count, scored_count = _unpack_counts(counts)

    st.divider()
    if attributes.empty:
        st.subheader("1. Lead attributes")
        st.warning(
            "This lead's attributes are no longer in `new_leads`: a later pipeline "
            "run ingested a new batch and replaced it. The decision this run made is "
            "still recorded in full and is shown below."
        )
    else:
        _render_attributes(attributes.iloc[0])

    st.divider()
    _render_outcome(decision, pool_entry)

    st.divider()
    _render_decision_trace(run_id, lead_id, decision, pool_entry, context, scored_count)

    st.divider()
    _render_eligibility(run_id, lead_id, eligible_count, has_rejections, rejected_count)


# --- helpers --------------------------------------------------------------

def _unpack_counts(counts: pd.DataFrame) -> tuple[int, int, int]:
    """Pull the three per-pair counts, defaulting to zero on an empty frame."""
    if counts.empty:
        return 0, 0, 0
    row = counts.iloc[0]
    return (
        int(row["eligible_count"]),
        int(row["rejected_count"]),
        int(row["scored_count"]),
    )


def _cap() -> int:
    return data.MAX_LEADS_PER_MANAGER


def _render_no_decisions(run_id: str) -> None:
    """Explain an empty trace, distinguishing an in-flight run from a finished one.

    A run selected while the pipeline is mid-flight has no assignments or pool rows
    yet. Saying "this run decided nothing" would read as a result rather than as a
    run that has not got there yet.
    """
    header = data.get_run_header(run_id)
    status = str(header.iloc[0]["status"]) if not header.empty else ""
    stage = fmt.na_or(header.iloc[0]["stage"]) if not header.empty else fmt.NA_DISPLAY

    if status == "running":
        st.info(
            f"This run is still in progress (current stage: `{stage}`). Assignments "
            "and pool entries appear once the optimizer and pool stages finish. "
            "Select a completed run in the sidebar to trace a lead now."
        )
    elif status == "failed":
        st.warning(
            f"This run failed at stage `{stage}` before producing any assignments "
            "or pool entries, so there is nothing to trace. See Pipeline Flow for "
            "the failure detail."
        )
    else:
        st.info("This run decided on no leads, so there is nothing to explain.")


def _load_display(load) -> str:
    """Render a load as ``n / cap``, or ``"N/A"`` when unknown."""
    if fmt.is_null(load):
        return fmt.NA_DISPLAY
    return f"{int(load)} / {_cap()}"


# --- lead selection -------------------------------------------------------

def _render_quick_picks(interesting: pd.DataFrame, available: list[str]) -> None:
    """Offer one lead per behaviour branch, so nothing has to be guessed.

    The middle button is the one that demonstrates the capacity fall-through: a
    lead whose best-scoring Agents were full and which therefore went to a
    lower-ranked one.
    """
    if interesting.empty:
        return
    row = interesting.iloc[0]
    picks = (
        (
            "top_confidence_lead_id",
            "First-choice assignment",
            "The lead the engine placed most decisively, on its best-scoring Agent.",
        ),
        (
            "fallthrough_lead_id",
            "Fell through to a lower choice",
            "Better-scoring Agents were already at the cap, so a lower-ranked Agent took it.",
        ),
        (
            "overflow_lead_id",
            "Pooled: every candidate full",
            "Eligible Agents existed but all were at the cap, so the lead went to the pool.",
        ),
    )

    st.markdown("**Jump to a representative lead**")
    columns = st.columns(len(picks))
    for column, (field, label, help_text) in zip(columns, picks, strict=False):
        lead = row[field] if field in row.index else None
        if fmt.is_null(lead):
            column.button(
                f"{label} (none in run)",
                disabled=True,
                use_container_width=True,
                key=f"pick_{field}",
            )
            continue
        if column.button(
            f"{label}\n\n`{lead}`",
            use_container_width=True,
            help=help_text,
            key=f"pick_{field}",
        ):
            st.session_state[LEAD_KEY] = lead

    # Preselect the most decisive assignment on first open so the view is useful
    # before anything is clicked.
    if st.session_state.get(LEAD_KEY) not in available:
        default = row["top_confidence_lead_id"]
        st.session_state[LEAD_KEY] = (
            default if not fmt.is_null(default) else available[0]
        )


def _render_lead_picker(available: list[str]) -> str | None:
    """Selectbox over the run's decided leads, plus a free-text override.

    The selectbox carries no ``key`` so the index recomputed from
    :data:`LEAD_KEY` actually moves the selection when a quick-pick is pressed.
    """
    current = st.session_state.get(LEAD_KEY)
    if current not in available:
        current = available[0]

    picker_col, manual_col = st.columns(2)
    selected = picker_col.selectbox(
        "Lead decided by this run",
        available,
        index=available.index(current),
        help=f"{len(available):,} leads were assigned or pooled by this run.",
    )
    manual = manual_col.text_input(
        "Or enter any lead_id",
        placeholder="lead_id from new_leads",
        key="explain_manual_lead",
    ).strip()

    if manual:
        # Deliberately not validated against the run's decided leads: entering an
        # id the run never decided on should produce the "not found" message.
        return manual

    st.session_state[LEAD_KEY] = selected
    return selected


# --- sections -------------------------------------------------------------

def _render_attributes(lead) -> None:
    """The lead's own attributes: the inputs eligibility matched against."""
    st.subheader("1. Lead attributes")
    st.caption("What eligibility matched Agents against.")
    intent_col, geo_col, lang_col, product_col = st.columns(4)
    intent_col.metric("Intent", fmt.na_or(lead["intent_bucket"]))
    geo_col.metric("Geography", fmt.na_or(lead["geography"]))
    lang_col.metric("Language", fmt.na_or(lead["language"]))
    product_col.metric("Product interest", fmt.na_or(lead["product_interest"]))


def _render_outcome(decision: pd.DataFrame, pool_entry: pd.DataFrame) -> None:
    """What the engine actually did with this lead."""
    st.subheader("2. Outcome")

    if not decision.empty:
        _render_assigned_outcome(decision.iloc[0])
        return
    if not pool_entry.empty:
        _render_pooled_outcome(pool_entry.iloc[0])
        return

    st.info(
        "This lead was neither assigned nor pooled on this run. That happens when "
        "validation rejected it, so it never reached eligibility."
    )


def _render_assigned_outcome(row) -> None:
    """Assigned: who got it, how confidently, and on what basis."""
    st.success(f"Assigned to Agent `{row['primary_manager_id']}`")

    primary_col, fallback_col, confidence_col, push_col = st.columns(4)
    primary_col.metric("Primary agent", str(row["primary_manager_id"]))
    primary_col.caption(f"load {_load_display(row['primary_date_load'])}")

    fallback_col.metric("Fallback agent", fmt.na_or(row["fallback_manager_id"]))
    if fmt.is_null(row["fallback_manager_id"]):
        fallback_col.caption("no second choice recorded")
    else:
        fallback_col.caption(f"load {_load_display(row['fallback_date_load'])}")

    confidence_col.metric(
        "Confidence",
        fmt.format_confidence(
            row["confidence_score"], row["match_score"], row["fallback_manager_id"]
        ),
    )
    confidence_col.caption(fmt.confidence_label(row["fallback_manager_id"]))

    push_col.metric("Push status", fmt.na_or(row["push_status"]))
    push_col.caption(f"assigned {fmt.format_timestamp(row['assigned_at'])}")

    # A stored confidence of exactly zero is the clipped case: the fallback
    # out-scored the primary, which means the primary won on availability rather
    # than on match quality. Worth saying out loud instead of showing a bare 0.
    if (
        not fmt.is_null(row["fallback_manager_id"])
        and not fmt.is_null(row["confidence_score"])
        and float(row["confidence_score"]) == 0.0
    ):
        st.warning(
            "Confidence is zero because the fallback Agent scored at least as high "
            "as the chosen one. The chosen Agent won on availability, not on match "
            "quality: the better-scoring Agent had no seat left."
        )

    if not fmt.is_null(row["fallback_manager_id"]) and not fmt.is_null(
        row["fallback_date_load"]
    ):
        if int(row["fallback_date_load"]) >= _cap():
            st.warning(
                f"The recorded fallback finished the business date at the "
                f"{_cap()}-lead cap, so a decline could not actually be routed "
                "there now. Fallbacks record the best alternative at the moment of "
                "assignment; later leads can fill that Agent afterwards."
            )


def _render_pooled_outcome(row) -> None:
    """Pooled: why it was not placed, and where it sits in the claim queue."""
    reason = str(row["reason"])
    if reason == "capacity_overflow":
        st.warning(
            f"Pooled as `capacity_overflow`: this lead had eligible, scored Agents, "
            f"but every one of them was already holding {_cap()} leads for the "
            "business date."
        )
    elif reason == "no_eligible_manager":
        st.warning(
            "Pooled as `no_eligible_manager`: no Agent matched this lead's "
            "language, geography, and product at all, so it was never scored."
        )
    else:
        st.warning(f"Pooled with reason `{fmt.na_or(row['reason'])}`.")

    rank_col, score_col, status_col, claimed_col = st.columns(4)
    rank_col.metric("Priority rank", fmt.na_or(row["priority_rank"]))
    rank_col.caption("1 = first in the claim queue")
    score_col.metric("Best score", fmt.format_probability(row["best_score"]))
    if fmt.is_null(row["best_score"]):
        score_col.caption("never scored")
    status_col.metric("Status", fmt.na_or(row["status"]))
    claimed_col.metric("Claimed by", fmt.na_or(row["claimed_by"]))
    claimed_col.caption(f"at {fmt.format_timestamp(row['claimed_at'])}")


def _render_decision_trace(
    run_id: str,
    lead_id: str,
    decision: pd.DataFrame,
    pool_entry: pd.DataFrame,
    context: pd.DataFrame,
    scored_count: int,
) -> None:
    """The ranked candidates and why the winner won.

    This is the heart of the view. The optimizer takes the first candidate with a
    free seat, so showing the ranking next to each candidate's load makes the
    decision reconstructable rather than asserted.
    """
    st.subheader("3. Decision trace")

    if not scored_count:
        st.info(
            "This lead was never scored, so there is no candidate ranking. That is "
            "consistent with a `no_eligible_manager` outcome."
        )
        return

    _render_trace_explanation(decision, pool_entry, context)

    limit, offset = page_controls(
        scored_count, CANDIDATE_PAGE_SIZE, key=f"candidates_{run_id}_{lead_id}"
    )
    candidates = data.get_lead_scores(run_id, lead_id, limit, offset, _cap())
    if candidates.empty:
        st.info("No candidates on this page.")
        return

    primary_id = decision.iloc[0]["primary_manager_id"] if not decision.empty else None
    fallback_id = decision.iloc[0]["fallback_manager_id"] if not decision.empty else None

    st.dataframe(
        _candidates_display(candidates, primary_id, fallback_id),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        f"Load is the Agent's total for the run's business date, which is the window "
        f"the {_cap()}-lead cap applies over. It counts optimizer assignments plus "
        "pool claims across every run sharing that date, so it can exceed this "
        "run's own count."
    )


def _render_trace_explanation(
    decision: pd.DataFrame, pool_entry: pd.DataFrame, context: pd.DataFrame
) -> None:
    """State in words why the chosen Agent was chosen."""
    if context.empty:
        return
    ctx = context.iloc[0]
    candidate_count = int(ctx["candidate_count"])
    primary_rank = ctx["primary_rank"]
    skipped_full = int(ctx["higher_ranked_at_cap"] or 0)

    if not decision.empty and not fmt.is_null(primary_rank):
        rank = int(primary_rank)
        if rank == 1:
            st.success(
                f"Went to its best-scoring Agent, ranked 1 of {candidate_count} "
                "candidates. No capacity constraint applied."
            )
            return

        skipped = rank - 1
        if skipped_full == skipped:
            st.info(
                f"Assigned to candidate **{rank} of {candidate_count}**. The "
                f"{skipped} better-scoring Agent(s) above it were all at the "
                f"{_cap()}-lead cap, so the engine fell through to the best Agent "
                "that still had a seat. This is the second-choice behaviour working."
            )
        else:
            # Be precise rather than tidy: end-of-date load does not account for
            # every skip, so do not claim it does.
            st.info(
                f"Assigned to candidate **{rank} of {candidate_count}**, skipping "
                f"{skipped} better-scoring Agent(s), of which {skipped_full} "
                f"finished the business date at the {_cap()}-lead cap. The "
                "remainder were full at the moment this lead was processed; the "
                "table below shows end-of-date load, not load at that instant."
            )
        return

    if not pool_entry.empty and str(pool_entry.iloc[0]["reason"]) == "capacity_overflow":
        st.warning(
            f"None of the {candidate_count} scored candidate(s) had a seat left, so "
            "the lead could not be placed and went to the pool instead."
        )


def _candidates_display(
    candidates: pd.DataFrame, primary_id, fallback_id
) -> pd.DataFrame:
    """Format the candidate ranking, marking the chosen and fallback Agents."""

    def role(manager_id: str) -> str:
        if primary_id is not None and manager_id == primary_id:
            return "chosen"
        if fallback_id is not None and manager_id == fallback_id:
            return "fallback"
        return ""

    return pd.DataFrame(
        {
            "Rank": candidates["rank"],
            "Agent (manager_id)": candidates["manager_id"],
            "Conversion probability": candidates["conversion_probability"].map(
                fmt.format_probability
            ),
            "Role": candidates["manager_id"].map(role),
            "Load (business date)": candidates["date_load"].map(_load_display),
            "In this run": candidates["run_load"],
            "Capacity": candidates["at_cap"].map(
                lambda full: "full" if bool(full) else "available"
            ),
        }
    )


def _render_eligibility(
    run_id: str,
    lead_id: str,
    eligible_count: int,
    has_rejections: bool,
    rejected_count: int,
) -> None:
    """Eligibility detail: who passed, and who was ruled out where recorded."""
    st.subheader("4. Eligibility")
    st.caption(SAMPLING_CAVEAT)

    if not eligible_count:
        st.warning(
            "No Agent was eligible for this lead, which is why it went to the pool "
            "with reason `no_eligible_manager`."
        )
    else:
        with st.expander(f"Eligible agents ({eligible_count:,})"):
            limit, offset = page_controls(
                eligible_count, ELIGIBLE_PAGE_SIZE, key=f"eligible_{run_id}_{lead_id}"
            )
            eligible = data.get_eligible_agents(run_id, lead_id, limit, offset)
            if eligible.empty:
                st.info("No eligible agents on this page.")
            else:
                st.dataframe(
                    eligible.rename(columns={"manager_id": "Agent (manager_id)"}),
                    use_container_width=True,
                    hide_index=True,
                )

    if not has_rejections:
        st.info(
            "Rejection detail was not sampled for this lead, so the reasons Agents "
            "were ruled out were not recorded on this run. This does not mean no "
            "Agent was rejected."
        )
        return

    with st.expander(f"Rejected agents ({rejected_count:,} sampled)", expanded=True):
        limit, offset = page_controls(
            rejected_count, REJECTION_PAGE_SIZE, key=f"rejections_{run_id}_{lead_id}"
        )
        rejections = data.get_sampled_rejections(run_id, lead_id, limit, offset)
        if rejections.empty:
            st.info("No rejections on this page.")
            return
        st.dataframe(
            pd.DataFrame(
                {
                    "Agent (manager_id)": rejections["manager_id"],
                    "Rejection reason": rejections["rejection_reason"].map(fmt.na_or),
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
