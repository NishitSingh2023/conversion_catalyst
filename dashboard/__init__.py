"""Read-only Streamlit dashboard for the Lead Assignment Engine.

Everything under this package is a *view* onto what the nightly pipeline already
wrote. No module here mutates pipeline state: the only database entry point is
``shared.db.read_sql``, reached exclusively from :mod:`dashboard.data`. The write
helpers in ``shared.db`` are deliberately never imported anywhere under
``dashboard/``, which keeps the read-only guarantee structural rather than a
convention someone has to remember.

Layout:
    * :mod:`dashboard.app` -- entry point: connection bootstrap, sidebar run
      selector, ``st.navigation`` dispatch.
    * :mod:`dashboard.data` -- query layer: parameterized, run-scoped reads.
    * :mod:`dashboard.format` -- pure formatting helpers (no Streamlit, no DB).
    * :mod:`dashboard.pages` -- the seven view modules.
"""
