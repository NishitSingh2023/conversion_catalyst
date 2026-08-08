"""Shared prev/next page controls for the paged views.

Three views (Assignments, Pool, Explainability) page over tables that can hold
hundreds of thousands of rows, and all three need the identical control: a page
counter bounded by a SQL ``count(*)``, with prev/next that cannot walk off either
end. Implementing it once here keeps the bound logic in one place rather than
triplicated, and keeps the pure arithmetic in :mod:`dashboard.format` where it is
property-tested.

This module renders widgets, so unlike ``format.py`` it does import Streamlit. It
issues no queries of its own; it only computes the ``:limit`` / ``:offset`` its
caller passes to :mod:`dashboard.data`.
"""
from __future__ import annotations

import streamlit as st

from dashboard import format as fmt


def page_controls(total_rows: int, page_size: int, key: str) -> tuple[int, int]:
    """Render prev/next controls and return the ``(limit, offset)`` to query.

    ``key`` should include the ``run_id`` so switching runs starts a fresh page
    counter instead of stranding the view on a page that no longer exists. The
    page index is kept under a non-widget session key, so it can be safely
    clamped and written on the same rerun that a button was clicked.
    """
    pages = fmt.page_count(total_rows, page_size)
    state_key = f"_page_index_{key}"
    current = min(max(0, int(st.session_state.get(state_key, 0))), pages - 1)

    prev_col, label_col, next_col = st.columns([1, 2, 1], vertical_alignment="center")
    if prev_col.button(
        "Previous", key=f"{key}_prev", disabled=current == 0, use_container_width=True
    ):
        current -= 1
    if next_col.button(
        "Next", key=f"{key}_next", disabled=current >= pages - 1, use_container_width=True
    ):
        current += 1

    current = min(max(0, current), pages - 1)
    st.session_state[state_key] = current

    first_row = current * page_size + 1 if total_rows else 0
    last_row = min((current + 1) * page_size, total_rows)
    label_col.markdown(
        f"<div style='text-align:center'>Page {current + 1} of {pages}"
        f"<br><span style='color:grey;font-size:0.85em'>"
        f"rows {first_row}-{last_row} of {total_rows}</span></div>",
        unsafe_allow_html=True,
    )

    return fmt.page_window(current, page_size)
