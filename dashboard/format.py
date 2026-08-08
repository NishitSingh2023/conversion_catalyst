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
