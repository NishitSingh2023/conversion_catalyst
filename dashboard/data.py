"""Read-only data-access layer for the dashboard.

This is the *only* module in the dashboard that talks to Postgres, and it does so
through a single entry point: ``shared.db.read_sql``. Nothing else from
``shared.db`` is imported, so no write path is reachable from the dashboard even
by accident -- the read-only contract is enforced by what is importable here
rather than by review discipline.

Conventions every query in this module follows:

  * One public function per query, taking explicit parameters bound as ``:name``
    placeholders -- never string-interpolated SQL.
  * Only ``SELECT`` statements (or ``SELECT``-only CTEs). No statement that
    mutates rows or schema exists in this module.
  * Run-scoped reads filter on ``WHERE run_id = :run_id`` so every view shows one
    consistent run.
  * Aggregates and counts are computed in SQL, not in pandas, and per-pair tables
    (``scores``, ``eligibility_matrix``) are always read with a ``:limit`` /
    ``:offset`` bound.
  * Agents are identified by ``manager_id`` only; the personal-name column on
    ``manager_profiles`` is never selected, so it cannot be rendered or logged.
  * Configuration is read through ``shared.config`` for the non-secret
    host/port/dbname used in the connection-failure message only. No credential
    is ever read, returned, or logged.

Populated by task 4 (connection probe, caching conventions, and the per-view
queries).
"""
from __future__ import annotations
