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

De-identification, and why it is a toggle
-----------------------------------------
The hackathon/demo deployment target is a **shared AWS lab account that must not
hold personal data**, so by default this adapter never writes a real rep name or
a real CRM identifier into Postgres: rep names are replaced by stable
``Agent-NNNN`` labels and ``PROSPECTID`` / ``REP_ID`` are replaced by salted
SHA-256 pseudonyms.

It has to stay switchable, though. The production LSQ bulk push writes
assignments back to the CRM keyed on the **real ProspectID** - a pseudonym would
update nothing. So anonymisation is a toggle (``ANONYMIZE_REAL_DATA`` env var,
default on; ``anonymize=`` argument per call) rather than a hard-coded rewrite:
ON for the lab account, OFF for a real CRM-connected deployment.

Nothing here can move the model: the trained features are the lead categoricals,
the manager aggregate rates and the match flags (see ``shared.constants``).
``lead_id``, ``manager_id`` and ``manager_name`` are join keys and display
labels, never features, so pseudonymising them is train/serve-neutral. What it
must preserve is *referential integrity* - the mapping is deterministic, so the
same input id yields the same pseudonym within a file, across both files and
across runs, and every join (history <-> new_leads <-> scores <-> assignments)
keeps working.

Mapping summary
---------------
lead_rep_dataset.csv  -> lead_manager_history
    PROSPECTID                  -> lead_id             (salted SHA-256 when anonymised)
    REP_ID                      -> manager_id          (salted SHA-256 when anonymised)
    REP_ID                      -> manager_name        ("Agent-NNNN" label when
                                                        anonymised, else REP_NAME;
                                                        display only, id is key)
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
    PROSPECTID                  -> lead_id              (deduplicated; salted
                                                         SHA-256 when anonymised,
                                                         same mapping as history)
    PRED_CATEGORY_WITH_SALES    -> intent_bucket        (null -> "EL")
    LEAD_MX_STATE               -> geography            (title-cased)
    LEAD_FINAL_PREFERRED_LANGUAGE -> language           (typo-normalised)
    LEAD_EXAM                   -> product_interest     (coarse exam token)
    LEAD_SOURCE                 -> lead_source
    LEAD_GRADE                  -> grade                ("grade_" stripped)
    (none)                      -> parent_student       (not in dataset -> null)

Every other source column is intentionally **not** mapped, and that includes all
of the free-text / demographic / financial PII the CSVs carry: LEAD_NOTES,
PB_NOTES, LEAD_SCHOOL, LEAD_PARENTAL_OCCUPATION, LEAD_PARENT_OCCUPATION,
LEAD_CITY, LEAD_FEE, LEAD_SCHOOL_FEE, LEAD_GENDER, MX_FEES, LEAD_PAID_AMOUNT and
the rep-locating REP_* columns. ``assert_no_pii_columns`` enforces that on the
way out of both adapters, so a future edit cannot quietly reintroduce one.
"""
from __future__ import annotations

import hashlib
import os
from datetime import date

import pandas as pd

from shared.constants import INTENT_BUCKETS

# --- De-identification ----------------------------------------------------
# Default ON: the shared lab account we deploy the demo into must not hold
# personal data. Set ANONYMIZE_REAL_DATA=0 for a production load where the LSQ
# bulk push has to write back against the real ProspectID.
_ANONYMIZE_ENV_VAR = "ANONYMIZE_REAL_DATA"
_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}

# The salt is *not* a secret and is not meant to be: pseudonymisation here exists
# so the demo dataset carries no names or CRM ids, not to make the mapping
# cryptographically unrecoverable (the id space is small enough to brute-force
# given the source file). It is an env var purely so a deployment can pick its
# own value; the default keeps local runs reproducible.
_SALT_ENV_VAR = "ANONYMIZE_SALT"
_DEFAULT_SALT = "conversion-catalyst-local"

# 12 hex chars = 48 bits. At ~5k leads / ~723 reps the chance of a collision is
# ~1e-9; adapt_* asserts there is none rather than trusting the arithmetic.
_ID_HEX_WIDTH = 12

# Readable agent labels live in a 4-digit space (Agent-0000..Agent-9999), which
# comfortably holds the ~723 reps. Rare collisions are resolved by deterministic
# linear probing over the pseudonymised ids in sorted order, so a given dataset
# always produces the same labels.
_AGENT_LABEL_SPACE = 10_000


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return default


#: Module-level default, read once at import. Callers can override per call with
#: the ``anonymize=`` argument (tests monkeypatch this attribute).
ANONYMIZE: bool = _env_flag(_ANONYMIZE_ENV_VAR, True)


def _salt() -> str:
    return os.getenv(_SALT_ENV_VAR) or _DEFAULT_SALT


def _resolve_anonymize(anonymize: bool | None) -> bool:
    """Explicit argument wins; otherwise fall back to the module default."""
    return ANONYMIZE if anonymize is None else bool(anonymize)


def pseudonymize_id(value: str, kind: str) -> str:
    """Salted-SHA-256 pseudonym for one identifier.

    ``kind`` namespaces the hash ("lead" vs "manager") so the two id spaces can
    never alias, while keeping the mapping a pure function of the input: the same
    PROSPECTID hashes identically in ``lead_rep_dataset.csv`` and
    ``leads_dataset_HML.csv``, which is what keeps the joins intact.
    """
    digest = hashlib.sha256(f"{_salt()}|{kind}|{value}".encode()).hexdigest()
    return digest[:_ID_HEX_WIDTH]


def _pseudonymize_series(series: pd.Series, kind: str) -> pd.Series:
    """Pseudonymise a series of ids, asserting the mapping stays injective."""
    present = series.dropna()
    mapping = {raw: pseudonymize_id(raw, kind) for raw in present.unique()}
    if len(set(mapping.values())) != len(mapping):
        raise AssertionError(
            f"{kind} pseudonymisation collided over {len(mapping)} ids; "
            f"widen _ID_HEX_WIDTH"
        )
    return series.map(mapping)


def agent_labels(manager_ids: pd.Series) -> dict[str, str]:
    """Map each (already pseudonymised) manager id to an ``Agent-NNNN`` label.

    Derived from the salted hash of the id, so it is stable across runs and both
    files. Names are for the agent-wise dashboard views only - no real rep name
    is ever stored.
    """
    labels: dict[str, str] = {}
    taken: set[int] = set()
    for mid in sorted(manager_ids.dropna().unique()):
        digest = hashlib.sha256(f"{_salt()}|agent-label|{mid}".encode()).hexdigest()
        slot = int(digest, 16) % _AGENT_LABEL_SPACE
        while slot in taken:
            slot = (slot + 1) % _AGENT_LABEL_SPACE
        taken.add(slot)
        labels[mid] = f"Agent-{slot:04d}"
    return labels


# Source columns that carry (or locate) a person and must never reach the
# adapter's output. Checked by name on the way out of both adapters.
PII_SOURCE_COLUMNS: frozenset[str] = frozenset(
    {
        # rep identity / location
        "REP_NAME",
        "REP_PRIMARY_CITY",
        "REP_PRIMARY_STATE",
        "REP_REGION",
        "REP_TEAM",
        "REP_DESIGNATION",
        # lead free text
        "LEAD_NOTES",
        "PB_NOTES",
        "LEAD_SUBJECTIVE_FEEDBACK_ON_NOTES",
        # lead demographics / institution
        "LEAD_SCHOOL",
        "LEAD_CITY",
        "LEAD_GENDER",
        "LEAD_PARENTAL_OCCUPATION",
        "LEAD_PARENT_OCCUPATION",
        # financials
        "LEAD_FEE",
        "LEAD_SCHOOL_FEE",
        "MX_FEES",
        "LEAD_PAID_AMOUNT",
        "REP_REVENUE",
    }
)


# Output columns that are identifiers/labels rather than features. Only these are
# value-checked against real rep names: feature columns legitimately contain
# place names, and at least one rep in the real file is named after a state
# ("Chandigarh"), which would make a blanket value scan a false positive.
_IDENTITY_COLUMNS: tuple[str, ...] = ("lead_id", "manager_id", "manager_name")


def assert_no_pii_columns(
    out: pd.DataFrame, source: pd.DataFrame | None = None, anonymize: bool | None = None
) -> None:
    """Fail loudly if adapter output carries PII.

    Two checks: no known-PII source column name survived into the output, and -
    when anonymising and a source frame is supplied - no real REP_NAME value
    reached an identifier column, which would mean a pseudonymisation path was
    bypassed.
    """
    leaked_columns = PII_SOURCE_COLUMNS.intersection(out.columns)
    if leaked_columns:
        raise AssertionError(
            f"PII columns present in adapter output: {sorted(leaked_columns)}"
        )
    if source is None or "REP_NAME" not in source.columns:
        return
    if not _resolve_anonymize(anonymize):
        return
    real_names = {n for n in source["REP_NAME"].map(_clean_str).unique() if n}
    if not real_names:
        return
    for column in _IDENTITY_COLUMNS:
        if column not in out.columns or out[column].dtype != object:
            continue
        if real_names.intersection(out[column].dropna().unique()):
            raise AssertionError(
                f"real REP_NAME values leaked into adapter output column {column!r}"
            )


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


def adapt_history(df: pd.DataFrame, anonymize: bool | None = None) -> pd.DataFrame:
    """Map ``lead_rep_dataset.csv`` onto the ``lead_manager_history`` schema.

    ``anonymize`` (default: the ``ANONYMIZE_REAL_DATA`` module setting) swaps the
    real CRM ids for salted hashes and the real REP_NAME for an ``Agent-NNNN``
    label. Turn it off only where the real ProspectID is needed downstream, i.e.
    a production load feeding the LSQ bulk push.
    """
    anonymize = _resolve_anonymize(anonymize)

    lead_id = df["PROSPECTID"].map(_clean_str)
    manager_id = df["REP_ID"].map(_clean_str)
    if anonymize:
        lead_id = _pseudonymize_series(lead_id, "lead")
        manager_id = _pseudonymize_series(manager_id, "manager")
        # Display label derived from the pseudonym - REP_NAME is not read.
        manager_name = manager_id.map(agent_labels(manager_id))
    else:
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
            "lead_id": lead_id,
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
    assert_no_pii_columns(out, df, anonymize=anonymize)
    return out


def adapt_new_leads(
    df: pd.DataFrame, batch_id: str | None = None, anonymize: bool | None = None
) -> pd.DataFrame:
    """Map ``leads_dataset_HML.csv`` onto the ``new_leads`` schema.

    Duplicated ``PROSPECTID`` rows are collapsed (first kept) because
    ``new_leads.lead_id`` is the primary key.

    ``anonymize`` behaves as in :func:`adapt_history` and uses the *same* lead
    pseudonym function, so a lead that appears in both files gets the same
    ``lead_id`` on both sides.
    """
    anonymize = _resolve_anonymize(anonymize)
    batch_id = batch_id or f"real-{date.today().isoformat()}"
    exam = df.get("LEAD_EXAM", pd.Series([None] * len(df), index=df.index))

    lead_id = df["PROSPECTID"].map(_clean_str)
    if anonymize:
        lead_id = _pseudonymize_series(lead_id, "lead")

    out = pd.DataFrame(
        {
            "lead_id": lead_id,
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
    assert_no_pii_columns(out, df, anonymize=anonymize)
    return out
