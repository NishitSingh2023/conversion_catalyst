"""Model view: which model produced the scores behind the assignments (Req 5).

Shows the single active model in ``model_registry`` with its held-out metrics.
The caveat about metric variance is permanent rather than conditional: with a
small positive class, AUC and precision move a lot between retrains, and a judge
reading a single number deserves to know that up front.
"""
from __future__ import annotations

import streamlit as st

from dashboard import data
from dashboard import format as fmt

METRIC_VARIANCE_CAVEAT = (
    "Model metrics are high-variance when the positive class is small: with few "
    "conversions in the held-out split, AUC, precision, and recall can shift "
    "noticeably between retrains. Read them as an indication of signal, not as a "
    "precise measurement."
)


def render() -> None:
    st.title("Model")

    with st.spinner("Loading active model..."):
        model = data.get_active_model()

    if model.empty:
        st.info("No active model is available.")
        st.caption(
            "Train and register a model with "
            "`DB_PORT=5433 python training/train.py` to populate this view."
        )
        return

    row = model.iloc[0]

    st.subheader(fmt.na_or(row["model_id"]))
    st.caption(f"Trained at {fmt.format_timestamp(row['trained_at'])}")

    auc_col, precision_col, recall_col, rows_col = st.columns(4)
    # Metrics are 0..1 probabilities; a null metric reads "N/A" rather than 0,
    # which would wrongly suggest the model scored zero.
    auc_col.metric("AUC", fmt.format_probability(row["auc"]))
    precision_col.metric("Precision", fmt.format_probability(row["precision"]))
    recall_col.metric("Recall", fmt.format_probability(row["recall"]))
    rows_col.metric("Training rows", _format_count(row["training_rows"]))

    st.caption(METRIC_VARIANCE_CAVEAT)

    _render_feature_list(row["feature_list"])

    st.divider()
    st.caption(
        "This read is cached for five minutes. Use **Refresh cached data** in the "
        "sidebar to re-query immediately after a retrain."
    )


def _format_count(value) -> str:
    """Thousands-separated integer, or ``"N/A"`` when the count is null."""
    if fmt.is_null(value):
        return fmt.NA_DISPLAY
    return f"{int(value):,}"


def _render_feature_list(feature_list) -> None:
    """Render the JSONB feature list, which is what the model actually consumed."""
    if fmt.is_null(feature_list):
        st.caption(f"Feature list: {fmt.NA_DISPLAY}")
        return

    features = list(feature_list) if isinstance(feature_list, list | tuple) else [feature_list]
    with st.expander(f"Features used ({len(features)})"):
        if not features:
            st.caption(fmt.EMPTY_ARRAY_DISPLAY)
            return
        # Two columns keeps a long one-hot encoded list readable.
        midpoint = (len(features) + 1) // 2
        left, right = st.columns(2)
        for column, chunk in ((left, features[:midpoint]), (right, features[midpoint:])):
            for feature in chunk:
                column.markdown(f"- `{feature}`")
