"""View modules for the read-only dashboard, one per requirement area.

Named ``views/`` rather than ``pages/`` on purpose. Streamlit treats a ``pages/``
directory sitting beside the entry point as the legacy filename-based multipage
convention and warns that it "may cause unusual app behavior" when
``st.navigation`` is also used, which it is: :mod:`dashboard.app` registers these
views explicitly so it can run the shared run selector before any view renders.
Renaming the package removes the conflict outright.

Each page reads ``st.session_state["selected_run"]`` and calls
:mod:`dashboard.data` for its rows, so every view renders a consistent picture of
one pipeline run. Pages never touch ``shared.db`` directly and never issue a
write.
"""
