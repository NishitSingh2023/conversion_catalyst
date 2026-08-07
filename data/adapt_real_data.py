#!/usr/bin/env python
"""Adapt the real team datasets onto the pipeline's canonical schema.

The team-provided CSVs (``lead_rep_dataset.csv`` and ``leads_dataset_HML.csv``)
share no column names with the tables the pipeline expects, and - more subtly -
encode the *same* concept differently on each side (``"10"`` vs ``"grade_10"``,
``"KARNATAKA"`` vs ``"karnataka"``, ``"Telgu"`` vs ``"Telugu"``). Loading them
raw would both fail the schema and, where it did not, quietly destroy the model
through train/serve skew: a one-hot column built from ``"grade_10"`` never lines
up with one built from ``"10"``.

This module is the single place that mapping and normalisation happens, so the
history side (training) and the new-leads side (serving) are cleaned by the
*same* functions and therefore stay aligned.

Mapping summary
---------------
lead_rep_dataset.csv  -> lead_manager_history
    PROSPECTID                  -> lead_id
    REP_ID                      -> manager_id
    REP_NAME                    -> manager_name        (display only; id is key)
    LEAD_PREDICTION_CATEGORY    -> lead_intent_bucket   (null -> "EL")
    LEAD_STATE                  -> lead_geography       (title-cased)
    LEAD_FINAL_PREFERRED_LANGUAGE -> lead_language      (typo-normalised)
    REP_TARGET_EXAM             -> lead_product         (coarse exam token)
    LEAD_SOURCE                 -> lead_source
    LEAD_GRADE                  -> lead_grade           ("grade_"/".0" stripped)
    LEAD_TOTAL_ATTEMPTED_CALLS  -> contact_attempts
    (none)                      -> first_response_mins  (not in dataset -> null)
    LEAD_CONVERTED_SALE         -> converted
    LEAD_GENERATED_DATE         -> interaction_date

leads_dataset_HML.csv -> new_leads
    PROSPECTID                  -> lead_id              (deduplicated)
    PRED_CATEGORY_WITH_SALES    -> intent_bucket        (null -> "EL")
    LEAD_MX_STATE               -> geography            (title-cased)
    LEAD_FINAL_PREFERRED_LANGUAGE -> language           (typo-normalised)
    LEAD_EXAM                   -> product_interest     (coarse exam token)
    LEAD_SOURCE                 -> lead_source
    LEAD_GRADE                  -> grade                ("grade_" stripped)
    (none)                      -> parent_student       (not in dataset -> null)
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from shared.constants import INTENT_BUCKETS

# Intent bucket used when the upstream category is missing. 36% of history rows
# have no LEAD_PREDICTION_CATEGORY yet a large share of the (already rare)
# conversions live in them, so dropping them would gut the positive class.
# Folding them into the lowest-intent bucket keeps the labels and stays inside
# the H/M/L/EL vocabulary the rest of the pipeline assumes.
_DEFAULT_INTENT = "EL"

# Language spellings vary across and within the two files. Normalise to one
# canonical spelling per language so match features compare like with like.
_LANGUAGE_CANONICAL = {
    "hindi": "Hindi",
    "english": "English",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "telgu": "Telugu",
    "kannada": "Kannada",
    "marathi": "Marathi",
    "bengali": "Bengali",
    "bangla": "Bengali",
    "gujarati": "Gujarati",
    "punjabi": "Punjabi",
    "odia": "Odia",
    "oriya": "Odia",
    "assamese": "Assamese",
    "malayalam": "Malayalam",
}
# Tokens that carry no language information.
_NULL_TOKENS = {"", "unknown", "nan", "none", "null", "na"}


def _clean_str(value) -> str | None:
    """Trim whitespace; treat blanks and null-ish tokens as missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s.lower() in _NULL_TOKENS:
        return None
    return s or None


def normalize_intent(value) -> str:
    """Map an intent label onto H/M/L/EL; unknown/missing -> EL."""
    s = _clean_str(value)
    if s is None:
        return _DEFAULT_INTENT
    up = s.upper()
    return up if up in INTENT_BUCKETS else _DEFAULT_INTENT


def normalize_geography(value) -> str | None:
    """Title-case state names so case-only variants collapse together."""
    s = _clean_str(value)
    return s.title() if s else None


def normalize_language(value) -> str | None:
    """Collapse spelling variants/typos to one canonical language name."""
    s = _clean_str(value)
    if s is None:
        return None
    return _LANGUAGE_CANONICAL.get(s.lower(), s.title())


def normalize_grade(value) -> str | None:
    """Strip the ``grade_`` prefix and ``.0`` float artefacts to a bare number.

    History stores ``"10"`` / ``"13.0"``; new-leads store ``"grade_10"``. Both
    must reduce to the same token (``"10"``) or grade never matches at serve
    time.
    """
    s = _clean_str(value)
    if s is None:
        return None
    s = s.lower().removeprefix("grade_").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s or None


def normalize_product(value) -> str:
    """Reduce a free-text exam/course string to a coarse, consistent token.

    The two files name the product differently (``REP_TARGET_EXAM`` vs
    ``LEAD_EXAM``) and with different granularity. Bucketing both through the
    same keyword rules keeps the ``product_overlap`` feature meaningful across
    train and serve.
    """
    s = _clean_str(value)
    if s is None:
        return "OTHER"
    up = s.upper()
    if "JEE" in up:
        return "JEE"
    if "NEET" in up or "MEDICAL" in up:
        return "NEET"
    if "FOUNDATION" in up:
        return "FOUNDATION"
    if "OLYMPIAD" in up:
        return "OLYMPIAD"
    if "CBSE" in up or "SCHOOL" in up or "BOARD" in up:
        return "CBSE"
    return "OTHER"


def _to_int(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0).astype(int)


# Column signatures used to auto-detect a real (unadapted) file.
_HISTORY_MARKERS = {"PROSPECTID", "REP_ID", "LEAD_CONVERTED_SALE"}
_NEW_LEADS_MARKERS = {"PROSPECTID", "PRED_CATEGORY_WITH_SALES"}


def is_real_history(df: pd.DataFrame) -> bool:
    return _HISTORY_MARKERS.issubset(df.columns)


def is_real_new_leads(df: pd.DataFrame) -> bool:
    return _NEW_LEADS_MARKERS.issubset(df.columns)


def adapt_history(df: pd.DataFrame) -> pd.DataFrame:
    """Map ``lead_rep_dataset.csv`` onto the ``lead_manager_history`` schema."""
    manager_id = df["REP_ID"].map(_clean_str)
    manager_name = df["REP_NAME"].map(_clean_str)

    # contact_attempts: prefer attempted-calls, fall back to raw call count.
    attempts = df.get("LEAD_TOTAL_ATTEMPTED_CALLS")
    if attempts is None:
        attempts = df.get("LEAD_CALL_COUNT", pd.Series(0, index=df.index))

    # interaction_date is NOT NULL; coalesce unparseable dates to today.
    interaction = pd.to_datetime(df["LEAD_GENERATED_DATE"], errors="coerce", format="mixed")
    interaction = interaction.dt.date.where(interaction.notna(), date.today())

    out = pd.DataFrame(
        {
            "lead_id": df["PROSPECTID"].map(_clean_str),
            "manager_id": manager_id,
            "manager_name": manager_name.fillna(manager_id),
            "lead_intent_bucket": df["LEAD_PREDICTION_CATEGORY"].map(normalize_intent),
            "lead_geography": df["LEAD_STATE"].map(normalize_geography),
            "lead_language": df["LEAD_FINAL_PREFERRED_LANGUAGE"].map(normalize_language),
            "lead_product": df["REP_TARGET_EXAM"].map(normalize_product),
            "lead_source": df["LEAD_SOURCE"].map(_clean_str),
            "lead_grade": df["LEAD_GRADE"].map(normalize_grade),
            "contact_attempts": _to_int(attempts),
            # Not present in the real dataset; a real float NaN maps cleanly to a
            # SQL NULL in a DOUBLE PRECISION column (pd.NA does not adapt).
            "first_response_mins": float("nan"),
            "converted": _to_int(df["LEAD_CONVERTED_SALE"]).astype(bool),
            "interaction_date": interaction,
        }
    )
    # A history row is meaningless without both a lead and a manager.
    out = out.dropna(subset=["lead_id", "manager_id"]).reset_index(drop=True)
    return out


def adapt_new_leads(df: pd.DataFrame, batch_id: str | None = None) -> pd.DataFrame:
    """Map ``leads_dataset_HML.csv`` onto the ``new_leads`` schema.

    Duplicated ``PROSPECTID`` rows are collapsed (first kept) because
    ``new_leads.lead_id`` is the primary key.
    """
    batch_id = batch_id or f"real-{date.today().isoformat()}"
    exam = df.get("LEAD_EXAM", pd.Series([None] * len(df), index=df.index))

    out = pd.DataFrame(
        {
            "lead_id": df["PROSPECTID"].map(_clean_str),
            "intent_bucket": df["PRED_CATEGORY_WITH_SALES"].map(normalize_intent),
            "geography": df["LEAD_MX_STATE"].map(normalize_geography),
            "language": df["LEAD_FINAL_PREFERRED_LANGUAGE"].map(normalize_language),
            "product_interest": exam.map(normalize_product),
            "lead_source": df["LEAD_SOURCE"].map(_clean_str),
            "grade": df["LEAD_GRADE"].map(normalize_grade),
            # Not present in the real dataset; None -> SQL NULL.
            "parent_student": None,
            "batch_id": batch_id,
        }
    )
    out = out.dropna(subset=["lead_id"])
    out = out.drop_duplicates(subset=["lead_id"], keep="first").reset_index(drop=True)
    return out
