"""View modules for the read-only dashboard, one per requirement area.

Each page reads ``st.session_state["selected_run"]`` and calls
:mod:`dashboard.data` for its rows, so every view renders a consistent picture of
one pipeline run. Pages never touch ``shared.db`` directly and never issue a
write.
"""
