"""Streamlit entry point for the read-only dashboard.

Run with ``streamlit run dashboard/app.py``.

This module exists so that run selection happens in exactly one place. Streamlit's
filename-based ``pages/`` convention offers no shared pre-render hook, which would
force every view to re-derive "which run am I showing?"; instead the bootstrap
here probes the connection, loads the run list, defaults the selection to the most
recent ``started_at``, stores it in ``st.session_state["selected_run"]``, and only
then dispatches to a view via ``st.navigation``. Every page therefore renders the
same run on the same rerun.

Responsibilities (task 6):
    * Probe the database through :mod:`dashboard.data`; on failure render a
      friendly message naming only the non-secret host, port, and database, with
      no stack trace and no credentials, then stop.
    * Render the sidebar run selector plus a "Refresh cached data" control.
    * Register the seven view pages, in order, and dispatch.

Like the rest of the package this is a read-only surface: it reaches the database
only through :mod:`dashboard.data`, which issues nothing but reads.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ``streamlit run dashboard/app.py`` puts *this file's* directory on sys.path, not
# the repo root, so neither ``dashboard`` nor ``shared`` is importable by default.
# Same bootstrap the other entrypoints use (scripts/run_pipeline.py,
# training/train.py), which keeps the documented run command working from any cwd
# without requiring PYTHONPATH to be set.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from dashboard import SELECTED_RUN_KEY, data  # noqa: E402
from dashboard.format import format_timestamp  # noqa: E402
from dashboard.views import (  # noqa: E402
    assignments,
    explainability,
    manager_profiles,
    model,
    pipeline_flow,
    pool,
    run_history,
)

PAGE_TITLE = "Lead Assignment Engine"

# The seven views, in the order judges should meet them: what the run did, the
# model behind it, the agents it routed to, the two outputs (assigned / pooled),
# the per-lead trace, then the history. Each entry is a render callable rather
# than a file path, so wiring is checked at import time and the legacy
# filename-based pages/ discovery never competes with st.navigation.
PAGE_SPECS: tuple[tuple[object, str, str], ...] = (
    (pipeline_flow.render, "Pipeline Flow", "pipeline-flow"),
    (model.render, "Model", "model"),
    (manager_profiles.render, "Manager Profiles", "manager-profiles"),
    (assignments.render, "Assignments", "assignments"),
    (pool.render, "Pool", "pool"),
    (explainability.render, "Explainability", "explainability"),
    (run_history.render, "Run History", "run-history"),
)


def build_pages() -> list[st.Page]:
    """Construct the ``st.Page`` list, first entry default."""
    return [
        st.Page(render, title=title, url_path=url_path, default=(idx == 0))
        for idx, (render, title, url_path) in enumerate(PAGE_SPECS)
    ]


def run_label(runs: pd.DataFrame, run_id: str) -> str:
    """Label a run as ``run_id | started_at | status`` for the selector."""
    row = runs.loc[runs["run_id"] == run_id]
    if row.empty:
        return run_id
    row = row.iloc[0]
    return f"{run_id} | {format_timestamp(row['started_at'])} | {row['status']}"


def render_sidebar(runs: pd.DataFrame, details: dict[str, object]) -> str:
    """Render the run selector and cache control; return the Selected_Run.

    The selectbox deliberately carries no ``key``. With no key, its identity
    includes the ``index`` argument, so when another view (Run History) writes
    ``selected_run`` and reruns, the recomputed index actually moves the
    selection. A keyed widget would instead pin itself to its own stored value,
    and writing that key from a page would raise, since the widget has already
    been instantiated earlier in the same rerun.
    """
    run_ids: list[str] = runs["run_id"].tolist()

    current = st.session_state.get(SELECTED_RUN_KEY)
    if current not in run_ids:
        # First load, or the previously selected run no longer exists: fall back
        # to the most recent run, which is the first row of the started_at DESC
        # query.
        current = run_ids[0]

    st.sidebar.header("Run")
    selected = st.sidebar.selectbox(
        "Pipeline run",
        run_ids,
        index=run_ids.index(current),
        format_func=lambda rid: run_label(runs, rid),
        help="Every view on this dashboard is scoped to the selected run.",
    )
    st.session_state[SELECTED_RUN_KEY] = selected

    st.sidebar.divider()
    if st.sidebar.button(
        "Refresh cached data",
        use_container_width=True,
        help="Clear cached reads (model, manager profiles, run list) and re-query.",
    ):
        st.cache_data.clear()
        st.rerun()

    # Non-secret connection identity only: host, port, database. No credential is
    # read here, so none can be rendered.
    st.sidebar.caption(
        f"Read-only connection to {details['dbname']} "
        f"at {details['host']}:{details['port']}"
    )
    return selected


def main() -> None:
    """Bootstrap the dashboard: probe, select a run, dispatch to a view."""
    st.set_page_config(page_title=PAGE_TITLE, layout="wide")

    ok, details = data.probe_connection()
    if not ok:
        st.title(PAGE_TITLE)
        # Names the database, host, and port so the operator can fix it, and
        # nothing else: no credential, no driver traceback.
        st.error(
            f"Cannot connect to database `{details['dbname']}` at "
            f"`{details['host']}:{details['port']}`."
        )
        st.caption(
            "Start the local database with `docker compose up -d postgres` and "
            "export `DB_PORT=5433`, then reload this page."
        )
        st.stop()

    runs = data.get_runs()
    if runs.empty:
        st.title(PAGE_TITLE)
        st.info("No runs are available.")
        st.caption(
            "Run the pipeline with `DB_PORT=5433 python scripts/run_pipeline.py` "
            "to populate this dashboard."
        )
        st.stop()

    render_sidebar(runs, details)
    st.navigation(build_pages()).run()


if __name__ == "__main__":
    main()
