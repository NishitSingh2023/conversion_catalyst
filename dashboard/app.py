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
