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
    LEAD_REGION (+ LEAD_CITY)   -> lead_geography       (canonical region, see below)
    LEAD_FINAL_PREFERRED_LANGUAGE -> lead_language      (typo-normalised)
    REP_TARGET_EXAM             -> lead_product         (coarse exam token)
    LEAD_SOURCE                 -> lead_source          (capped to the top 30, else "OTHER")
    LEAD_GRADE                  -> lead_grade           (integer 1..13, else null)
    LEAD_TOTAL_ATTEMPTED_CALLS  -> contact_attempts
    (none)                      -> first_response_mins  (not in dataset -> null)
    LEAD_CONVERTED_SALE         -> converted
    LEAD_GENERATED_DATE         -> interaction_date

leads_dataset_HML.csv -> new_leads
    PROSPECTID                  -> lead_id              (deduplicated; salted
                                                         SHA-256 when anonymised,
                                                         same mapping as history)
    PRED_CATEGORY_WITH_SALES    -> intent_bucket        (null -> "EL")
    LEAD_CITY (+ LEAD_MX_STATE) -> geography            (canonical region, see below)
    LEAD_FINAL_PREFERRED_LANGUAGE -> language           (typo-normalised)
    LEAD_EXAM                   -> product_interest     (coarse exam token)
    LEAD_SOURCE                 -> lead_source          (same top-30 cap as history)
    LEAD_GRADE                  -> grade                (integer 1..13, else null)
    (none)                      -> parent_student       (not in dataset -> null)

Every other source column is intentionally **not** mapped, and that includes all
of the free-text / demographic / financial PII the CSVs carry: LEAD_NOTES,
PB_NOTES, LEAD_SCHOOL, LEAD_PARENTAL_OCCUPATION, LEAD_PARENT_OCCUPATION,
LEAD_CITY, LEAD_FEE, LEAD_SCHOOL_FEE, LEAD_GENDER, MX_FEES, LEAD_PAID_AMOUNT and
the rep-locating REP_* columns. ``assert_no_pii_columns`` enforces that on the
way out of both adapters, so a future edit cannot quietly reintroduce one.
LEAD_CITY is read on both sides but only ever as an *intermediate lookup key*
(city -> region); the city itself never reaches the output.

Geography is a canonical REGION, and both sides share one lookup
--------------------------------------------------------------------
Geography is a **hard** eligibility filter (a manager only gets leads in a
territory they demonstrably work), so it has to be a value that (a) is populated
and (b) is coarse enough that a manager's history covers it. Neither state
column qualifies on the real file: LEAD_STATE is populated on 3.8% of history
rows, so ``manager_profiles.geographies_handled`` came out nearly empty and the
filter passed almost every lead. Worse, the two sides read *different* columns
(LEAD_STATE for training, LEAD_MX_STATE for serving), which is textbook
train/serve skew.

So geography is now one of eight canonical regions (see ``CANONICAL_REGIONS``),
derived through :class:`ReferenceData`:

    history   lead_geography = normalize_region(LEAD_REGION)
                               or city_to_region[LEAD_CITY]
    new leads geography      = city_to_region[LEAD_CITY]
                               or state_to_region[LEAD_MX_STATE]

new_leads has no LEAD_REGION at all, which is exactly why the lookups exist.
**Both lookups, and the LEAD_SOURCE cap, are built from the history file and then
applied to both sides inside the same load** (see ``scripts/load_sample_data.py``,
which scans history once to build a :class:`ReferenceData` and threads it through
both adapters). That coupling is deliberate and load-order matters: history must
be scanned before new leads are adapted, otherwise the two sides can label the
same city differently and every geography match feature becomes noise. Adapting
new leads with no reference is allowed but yields ``geography = None``, which the
ingest validator marks invalid - honest, rather than an invented region.

LEAD_CITY itself is deliberately *not* used as the geography value: 25k distinct
cities is far too narrow for a hard territory filter and would explode the
model's one-hot encoding.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
    # Single-letter and misspelled variants seen in the real history file.
    "e": "English",
    "gujrati": "Gujarati",
}
# Tokens that carry no language information.
_NULL_TOKENS = {"", "unknown", "nan", "none", "null", "na"}

# Zero-width / non-breaking characters that the source system leaves inside
# otherwise-clean values ("Northern\xa0\u200b"). They are invisible in a
# spreadsheet and produce a *distinct* one-hot column, so they are stripped
# everywhere rather than trimmed by ``str.strip`` alone (which does remove \xa0
# but not \u200b).
_INVISIBLE_CHARS = "\xa0\u200b\u200c\u200d\ufeff"
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")
_WHITESPACE_RE = re.compile(r"\s+")
# Both LEAD_REGION and LEAD_STATE_CODE carry a per-territory numeric suffix
# ("Southern_1423", "AP_0120"). It explodes 20 real regions into 3,761 values.
_NUMERIC_SUFFIX_RE = re.compile(r"_\d+$")


def _strip_invisible(s: str) -> str:
    """Remove zero-width/non-breaking characters and collapse whitespace runs."""
    return _WHITESPACE_RE.sub(" ", _INVISIBLE_RE.sub("", s)).strip()


def _clean_str(value) -> str | None:
    """Trim whitespace; treat blanks and null-ish tokens as missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = _strip_invisible(str(value))
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


def normalize_language(value) -> str | None:
    """Collapse spelling variants/typos to one canonical language name."""
    s = _clean_str(value)
    if s is None:
        return None
    return _LANGUAGE_CANONICAL.get(s.lower(), s.title())


# --- Grade ----------------------------------------------------------------
# The school grades the business actually sells to. Anything outside the range
# is not a grade: LEAD_GRADE has 1,012 distinct values in the real history file
# and the tail is phone numbers ("919640361652919640361652"), zero-padded junk
# ("08000900"), a 40-character run of zeros with unicode superscripts, school
# names and years ("2026"). Each of those would otherwise become its own one-hot
# column.
GRADE_MIN, GRADE_MAX = 1, 13
_NON_ASCII_DIGIT_RE = re.compile(r"[^0-9]")


def normalize_grade(value) -> str | None:
    """Reduce a grade to a bare integer token in ``GRADE_MIN..GRADE_MAX``.

    History stores ``"10"`` / ``"13.0"``; new leads store ``"grade_10"``. Both
    must reduce to the same token (``"10"``) or grade never matches at serve
    time. Suffixed spellings resolve too (``"11th"`` -> ``"11"``, ``"12+"`` and
    ``"12_pass"`` -> ``"12"``).

    Anything that does not resolve to a plausible grade returns ``None`` rather
    than a bogus category - a missing grade one-hots as "unknown", which is what
    it is.
    """
    s = _clean_str(value)
    if s is None:
        return None
    s = s.lower().removeprefix("grade_").strip()
    if s.endswith(".0"):
        s = s[:-2]
    # Only ASCII digits count: ``str.isdigit()`` is True for unicode superscripts
    # (which the real file contains) but ``int()`` rejects them.
    digits = _NON_ASCII_DIGIT_RE.sub("", s)
    if not digits:
        return None
    number = int(digits)
    if GRADE_MIN <= number <= GRADE_MAX:
        return str(number)
    return None


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


# ==========================================================================
# Geography: canonical regions and the history-derived lookups
# ==========================================================================

#: The whole geography vocabulary. Eight values across 953 managers means a
#: manager's history plausibly covers their region, which is what makes the hard
#: geography filter in eligibility survivable.
CANONICAL_REGIONS: tuple[str, ...] = (
    "North",
    "North East",
    "North & South",
    "South",
    "East",
    "West",
    "Central",
    "Islands",
)

# Every spelling of a region observed in LEAD_REGION after the numeric suffix and
# the invisible characters are stripped, keyed lowercase.
#
# Two judgement calls worth knowing about:
#   * "Assamern" (3,020 rows) is a misspelling of a region that also exists
#     properly ("North Eastern"); Assam is in the north east, so it folds there
#     rather than becoming its own bucket.
#   * "North & South" (293,685 rows) is NOT guessed away. It is far too common to
#     be noise and there is no basis for splitting it, so it is its own region.
#     It simply will not match a manager who only ever worked "North".
_REGION_CANONICAL: dict[str, str] = {
    "south": "South",
    "southern": "South",
    "north": "North",
    "northern": "North",
    "east": "East",
    "eastern": "East",
    "west": "West",
    "western": "West",
    "central": "Central",
    "north east": "North East",
    "north eastern": "North East",
    "north-east": "North East",
    "north-eastern": "North East",
    "northeast": "North East",
    "northeastern": "North East",
    "assamern": "North East",
    "north & south": "North & South",
    "north and south": "North & South",
    "special (island uts)": "Islands",
    "island uts": "Islands",
    "islands": "Islands",
}


def normalize_region(value) -> str | None:
    """Map a raw LEAD_REGION value onto one of :data:`CANONICAL_REGIONS`.

    Strips the trailing ``_<digits>`` territory suffix, the non-breaking /
    zero-width characters the source system embeds, and case, then looks the
    result up in a fixed table. **Unrecognised values return ``None``**, not an
    "other" bucket: geography is a hard eligibility filter, and a catch-all
    region would let managers match leads they have no territory claim over.
    """
    s = _clean_str(value)
    if s is None:
        return None
    s = _strip_invisible(_NUMERIC_SUFFIX_RE.sub("", s))
    return _REGION_CANONICAL.get(s.lower())


def city_lookup_key(value) -> str | None:
    """Normalised key for the city -> region lookup (upper-cased, trimmed)."""
    s = _clean_str(value)
    return s.upper() if s else None


def state_lookup_key(value) -> str | None:
    """Normalised key for the state -> region lookup.

    Squashed to letters and digits only, because the state columns disagree on
    separators and casing across the two files ("tamil_nadu", "TAMIL NADU",
    "TAMILNADU" all appear, and LEAD_STATE_CODE carries the same ``_1234``
    territory suffix as LEAD_REGION).
    """
    s = _clean_str(value)
    if s is None:
        return None
    key = re.sub(r"[^A-Z0-9]", "", _NUMERIC_SUFFIX_RE.sub("", s).upper())
    return key or None


def _source_lookup_key(value: str) -> str:
    """Case/whitespace-insensitive key for LEAD_SOURCE.

    ``"International Leads"`` (522 rows) and ``"International leads"`` (5 rows)
    are the same source; keying on the casefolded form stops one landing inside
    the top-30 cap and the other in "OTHER".
    """
    return _WHITESPACE_RE.sub(" ", value).strip().casefold()


#: How many LEAD_SOURCE values keep their own identity; the rest become "OTHER".
#: 30 covers 99.997% of history rows while bounding the one-hot width.
SOURCE_CAP: int = 30
#: Bucket for sources outside the cap.
OTHER_SOURCE: str = "OTHER"


@dataclass(frozen=True)
class ReferenceData:
    """Lookups derived from the *history* file and applied to both sides.

    Built by :class:`ReferenceBuilder` (streamable over chunks) so a 2.7M-row
    history file can be scanned once, cheaply, before anything is adapted. The
    same instance is then handed to :func:`adapt_history` and
    :func:`adapt_new_leads` in a single load, which is what guarantees the two
    sides cannot disagree about a city's region or a source's identity.

    Attributes
    ----------
    city_to_region / state_to_region:
        Normalised key -> canonical region, taking the *modal* region per key.
    allowed_sources:
        Normalised LEAD_SOURCE key -> the canonical spelling to emit. Keys absent
        from this mapping become :data:`OTHER_SOURCE`.
    manager_ids:
        Every raw REP_ID in the history file, sorted. Needed because
        ``Agent-NNNN`` labels are allocated by probing a shared 4-digit space:
        derived from one chunk's managers they would not agree with another's, so
        the label map is always built from the full id set.
    """

    city_to_region: Mapping[str, str] = field(default_factory=dict)
    state_to_region: Mapping[str, str] = field(default_factory=dict)
    allowed_sources: Mapping[str, str] = field(default_factory=dict)
    manager_ids: tuple[str, ...] = ()

    def region_for_city(self, value) -> str | None:
        key = city_lookup_key(value)
        return self.city_to_region.get(key) if key else None

    def region_for_state(self, value) -> str | None:
        key = state_lookup_key(value)
        return self.state_to_region.get(key) if key else None

    def canonical_source(self, value) -> str | None:
        """Cap a LEAD_SOURCE value; ``None`` stays ``None`` (missing, not OTHER)."""
        s = _clean_str(value)
        if s is None:
            return None
        if not self.allowed_sources:
            # No cap known (adapting a bare frame without a reference): pass the
            # value through rather than collapsing everything to OTHER.
            return s
        return self.allowed_sources.get(_source_lookup_key(s), OTHER_SOURCE)


#: Used when a caller adapts a frame without supplying a reference.
EMPTY_REFERENCE = ReferenceData()


def _modal(counts: Counter[str]) -> str:
    """Most frequent key, ties broken alphabetically so builds are reproducible."""
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


class ReferenceBuilder:
    """Accumulate the history-derived lookups over one or more chunks.

    Only five source columns are touched (LEAD_REGION, LEAD_CITY, LEAD_STATE,
    LEAD_STATE_CODE, LEAD_SOURCE) plus REP_ID, so the scan pass can read a narrow
    slice of the 70-column file. Counting is done with a groupby per chunk rather
    than a Python loop over rows, so the work scales with the number of *distinct*
    pairs (~43k) instead of the row count.
    """

    def __init__(self) -> None:
        self._city_regions: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self._state_regions: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self._sources: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self._manager_ids: set[str] = set()

    def add(self, df: pd.DataFrame) -> ReferenceBuilder:
        if "REP_ID" in df.columns:
            self._manager_ids.update(df["REP_ID"].map(_clean_str).dropna().unique())

        if "LEAD_SOURCE" in df.columns:
            for raw, n in df["LEAD_SOURCE"].map(_clean_str).dropna().value_counts().items():
                self._sources[_source_lookup_key(str(raw))][str(raw)] += int(n)

        if "LEAD_REGION" not in df.columns:
            return self
        region = df["LEAD_REGION"].map(normalize_region)
        if "LEAD_CITY" in df.columns:
            self._tally(self._city_regions, df["LEAD_CITY"].map(city_lookup_key), region)
        for column in ("LEAD_STATE", "LEAD_STATE_CODE"):
            if column in df.columns:
                self._tally(self._state_regions, df[column].map(state_lookup_key), region)
        return self

    @staticmethod
    def _tally(
        target: defaultdict[str, Counter[str]], keys: pd.Series, regions: pd.Series
    ) -> None:
        pairs = pd.DataFrame({"key": keys, "region": regions}).dropna()
        if pairs.empty:
            return
        for (key, region), n in pairs.groupby(["key", "region"], sort=False).size().items():
            target[str(key)][str(region)] += int(n)

    def build(self) -> ReferenceData:
        ranked = sorted(
            self._sources.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
        )[:SOURCE_CAP]
        return ReferenceData(
            city_to_region={city: _modal(c) for city, c in self._city_regions.items()},
            state_to_region={state: _modal(c) for state, c in self._state_regions.items()},
            allowed_sources={key: _modal(spellings) for key, spellings in ranked},
            manager_ids=tuple(sorted(self._manager_ids)),
        )


def build_reference_data(chunks: pd.DataFrame | Iterable[pd.DataFrame]) -> ReferenceData:
    """Build the history-derived lookups from a frame or an iterable of chunks."""
    builder = ReferenceBuilder()
    if isinstance(chunks, pd.DataFrame):
        builder.add(chunks)
    else:
        for chunk in chunks:
            builder.add(chunk)
    return builder.build()


#: Source columns the history adapter and the reference scan need. Used by the
#: loader to read a narrow slice of the 70-column file (``usecols``).
HISTORY_SOURCE_COLUMNS: tuple[str, ...] = (
    "PROSPECTID",
    "REP_ID",
    "REP_NAME",
    "LEAD_PREDICTION_CATEGORY",
    "LEAD_REGION",
    "LEAD_CITY",
    "LEAD_STATE",
    "LEAD_STATE_CODE",
    "LEAD_FINAL_PREFERRED_LANGUAGE",
    "REP_TARGET_EXAM",
    "LEAD_SOURCE",
    "LEAD_GRADE",
    "LEAD_TOTAL_ATTEMPTED_CALLS",
    "LEAD_CALL_COUNT",
    "LEAD_CONVERTED_SALE",
    "LEAD_GENERATED_DATE",
)

#: The subset of the above that the reference scan (first pass) reads.
REFERENCE_SCAN_COLUMNS: tuple[str, ...] = (
    "REP_ID",
    "LEAD_REGION",
    "LEAD_CITY",
    "LEAD_STATE",
    "LEAD_STATE_CODE",
    "LEAD_SOURCE",
)

#: Source columns the new-leads adapter needs.
NEW_LEADS_SOURCE_COLUMNS: tuple[str, ...] = (
    "PROSPECTID",
    "PRED_CATEGORY_WITH_SALES",
    "LEAD_MX_STATE",
    "LEAD_CITY",
    "LEAD_FINAL_PREFERRED_LANGUAGE",
    "LEAD_EXAM",
    "LEAD_SOURCE",
    "LEAD_GRADE",
)


# Column signatures used to auto-detect a real (unadapted) file.
_HISTORY_MARKERS = {"PROSPECTID", "REP_ID", "LEAD_CONVERTED_SALE"}
_NEW_LEADS_MARKERS = {"PROSPECTID", "PRED_CATEGORY_WITH_SALES"}


def _column_names(source: pd.DataFrame | Iterable[str]) -> set[str]:
    """Accept a frame or a bare header list, so a file can be sniffed without
    reading a single row of it (the history file is 1.29 GB)."""
    if isinstance(source, pd.DataFrame):
        return set(source.columns)
    return set(source)


def is_real_history(source: pd.DataFrame | Iterable[str]) -> bool:
    return _HISTORY_MARKERS.issubset(_column_names(source))


def is_real_new_leads(source: pd.DataFrame | Iterable[str]) -> bool:
    return _NEW_LEADS_MARKERS.issubset(_column_names(source))


#: LEAD_GENERATED_DATE is uniformly ``2026-04-30 17:38:43.000`` across all
#: 2,719,558 history rows. Naming the format turns date parsing from a
#: per-element inference (``format="mixed"``, minutes at this row count) into a
#: vectorised parse; anything the format cannot read falls back to inference
#: rather than being silently coalesced to today.
_HISTORY_DATE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _parse_history_dates(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", format=_HISTORY_DATE_FORMAT)
    unparsed = parsed.isna()
    if unparsed.any():
        parsed = parsed.fillna(
            pd.to_datetime(series[unparsed], errors="coerce", format="mixed")
        )
    return parsed


def _missing(df: pd.DataFrame) -> pd.Series:
    """An all-null object Series aligned to ``df`` (for absent source columns)."""
    return pd.Series([None] * len(df), index=df.index, dtype=object)


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else _missing(df)


def _history_geography(df: pd.DataFrame, reference: ReferenceData) -> pd.Series:
    """Canonical region per history row: LEAD_REGION, else the city lookup.

    LEAD_REGION is populated on 78% of rows and LEAD_CITY on 79%, and they are not
    the same 78%, so the fallback is worth having.
    """
    region = _column(df, "LEAD_REGION").map(normalize_region)
    by_city = _column(df, "LEAD_CITY").map(reference.region_for_city)
    return region.where(region.notna(), by_city)


def _new_leads_geography(df: pd.DataFrame, reference: ReferenceData) -> pd.Series:
    """Canonical region per new lead: the city lookup, else the state lookup.

    new_leads carries no LEAD_REGION, so both paths go through lookups built from
    history - which is precisely what keeps train and serve on one vocabulary.
    Unresolved stays ``None``; ingest marks the lead invalid rather than the
    loader inventing a territory.
    """
    by_city = _column(df, "LEAD_CITY").map(reference.region_for_city)
    by_state = _column(df, "LEAD_MX_STATE").map(reference.region_for_state)
    return by_city.where(by_city.notna(), by_state)


def _manager_label_map(reference: ReferenceData, manager_id: pd.Series) -> dict[str, str]:
    """``Agent-NNNN`` labels for the manager id space.

    Built from ``reference.manager_ids`` (every rep in the file) when available.
    Labels are allocated by probing a shared 4-digit space, and at 953 reps
    collisions are a near certainty, so deriving them from only the chunk in hand
    would give the same rep different labels in different chunks.
    """
    if reference.manager_ids:
        full = pd.Series([pseudonymize_id(m, "manager") for m in reference.manager_ids])
        return agent_labels(full)
    return agent_labels(manager_id)


def adapt_history(
    df: pd.DataFrame,
    anonymize: bool | None = None,
    reference: ReferenceData | None = None,
) -> pd.DataFrame:
    """Map ``lead_rep_dataset.csv`` onto the ``lead_manager_history`` schema.

    ``anonymize`` (default: the ``ANONYMIZE_REAL_DATA`` module setting) swaps the
    real CRM ids for salted hashes and the real REP_NAME for an ``Agent-NNNN``
    label. Turn it off only where the real ProspectID is needed downstream, i.e.
    a production load feeding the LSQ bulk push.

    ``reference`` supplies the city->region lookup, the LEAD_SOURCE cap and the
    full manager id space. Pass the *same* instance used for
    :func:`adapt_new_leads`, and - when loading in chunks - one built from the
    whole file, or per-chunk output will disagree. Omitting it builds a reference
    from ``df`` alone, which is only correct when ``df`` is the entire history.
    """
    anonymize = _resolve_anonymize(anonymize)
    if reference is None:
        reference = build_reference_data(df)

    lead_id = df["PROSPECTID"].map(_clean_str)
    manager_id = df["REP_ID"].map(_clean_str)
    if anonymize:
        lead_id = _pseudonymize_series(lead_id, "lead")
        manager_id = _pseudonymize_series(manager_id, "manager")
        # Display label derived from the pseudonym - REP_NAME is not read.
        manager_name = manager_id.map(_manager_label_map(reference, manager_id))
    else:
        manager_name = _column(df, "REP_NAME").map(_clean_str)

    # contact_attempts: prefer attempted-calls, fall back to raw call count.
    attempts = df.get("LEAD_TOTAL_ATTEMPTED_CALLS")
    if attempts is None:
        attempts = df.get("LEAD_CALL_COUNT", pd.Series(0, index=df.index))

    # interaction_date is NOT NULL; coalesce unparseable dates to today.
    interaction = _parse_history_dates(df["LEAD_GENERATED_DATE"])
    interaction = interaction.dt.date.where(interaction.notna(), date.today())

    out = pd.DataFrame(
        {
            "lead_id": lead_id,
            "manager_id": manager_id,
            "manager_name": manager_name.fillna(manager_id),
            "lead_intent_bucket": df["LEAD_PREDICTION_CATEGORY"].map(normalize_intent),
            "lead_geography": _history_geography(df, reference),
            "lead_language": df["LEAD_FINAL_PREFERRED_LANGUAGE"].map(normalize_language),
            "lead_product": df["REP_TARGET_EXAM"].map(normalize_product),
            "lead_source": df["LEAD_SOURCE"].map(reference.canonical_source),
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
    df: pd.DataFrame,
    batch_id: str | None = None,
    anonymize: bool | None = None,
    reference: ReferenceData | None = None,
) -> pd.DataFrame:
    """Map ``leads_dataset_HML.csv`` onto the ``new_leads`` schema.

    Duplicated ``PROSPECTID`` rows are collapsed (first kept) because
    ``new_leads.lead_id`` is the primary key.

    ``anonymize`` behaves as in :func:`adapt_history` and uses the *same* lead
    pseudonym function, so a lead that appears in both files gets the same
    ``lead_id`` on both sides.

    ``reference`` MUST be the one built from the history file - it is the only
    source of a region for these rows. Without it every ``geography`` is ``None``
    and ingest rejects the batch, which is the intended failure mode: silence is
    better than a fabricated territory.
    """
    anonymize = _resolve_anonymize(anonymize)
    reference = reference or EMPTY_REFERENCE
    batch_id = batch_id or f"real-{date.today().isoformat()}"

    lead_id = df["PROSPECTID"].map(_clean_str)
    if anonymize:
        lead_id = _pseudonymize_series(lead_id, "lead")

    out = pd.DataFrame(
        {
            "lead_id": lead_id,
            "intent_bucket": df["PRED_CATEGORY_WITH_SALES"].map(normalize_intent),
            "geography": _new_leads_geography(df, reference),
            "language": df["LEAD_FINAL_PREFERRED_LANGUAGE"].map(normalize_language),
            "product_interest": _column(df, "LEAD_EXAM").map(normalize_product),
            "lead_source": df["LEAD_SOURCE"].map(reference.canonical_source),
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


# ==========================================================================
# Demo batch sizing
# ==========================================================================

#: Fixed so a demo run is reproducible; overridable per call.
SAMPLE_SEED: int = 20260808
#: The column the sample is stratified on.
INTENT_STRATA_COLUMN: str = "PRED_CATEGORY_WITH_SALES"


def _stratum_quotas(sizes: Mapping[str, int], max_rows: int) -> dict[str, int]:
    """Split ``max_rows`` across strata proportionally (largest remainder).

    Every non-empty stratum keeps at least one row while the budget allows, so a
    small-but-important bucket cannot vanish. Ordering is alphabetical throughout
    so the result is reproducible.
    """
    total = sum(sizes.values())
    if total <= max_rows:
        return dict(sizes)

    order = sorted(sizes)
    exact = {k: sizes[k] * max_rows / total for k in order}
    quota = {k: min(sizes[k], int(exact[k])) for k in order}
    for k in order:
        if quota[k] == 0 and sizes[k] > 0 and sum(quota.values()) < max_rows:
            quota[k] = 1

    # Hand out what rounding left over, largest fractional part first.
    by_remainder = sorted(order, key=lambda k: (-(exact[k] % 1), k))
    while sum(quota.values()) < max_rows:
        progressed = False
        for k in by_remainder:
            if sum(quota.values()) >= max_rows:
                break
            if quota[k] < sizes[k]:
                quota[k] += 1
                progressed = True
        if not progressed:  # every stratum saturated
            break
    # The min-one guarantee can overshoot when strata are very lopsided; give the
    # excess back from the largest strata, never below one row.
    while sum(quota.values()) > max_rows:
        for k in sorted(order, key=lambda k: (-quota[k], k)):
            if sum(quota.values()) <= max_rows:
                break
            if quota[k] > 1:
                quota[k] -= 1
    return quota


def sample_new_leads(
    df: pd.DataFrame,
    max_rows: int,
    seed: int = SAMPLE_SEED,
    strata_column: str = INTENT_STRATA_COLUMN,
) -> pd.DataFrame:
    """Deduplicate on PROSPECTID and cut the batch down to ``max_rows``.

    Eligibility is a ``leads x managers`` cross join (150k x 953 = 143M pairs), so
    a demo runs on a slice. The slice is:

      * **deduplicated first** - the raw file has 452,679 rows for 150,594 leads,
        and sampling before dedupe would spend the budget on repeats;
      * **stratified on intent** - a uniform sample of a batch that is 12% H would
        wobble, and a demo that lost its high-intent leads shows nothing. Null
        intents are their own (EL) stratum, matching ``normalize_intent``;
      * **deterministic** - fixed seed, so two runs load the same leads.

    ``max_rows <= 0`` means "no cap".
    """
    if "PROSPECTID" in df.columns:
        df = df.drop_duplicates(subset=["PROSPECTID"], keep="first")
    if max_rows <= 0 or len(df) <= max_rows:
        return df.reset_index(drop=True)

    if strata_column not in df.columns:
        return df.sample(n=max_rows, random_state=seed).sort_index().reset_index(drop=True)

    strata = df[strata_column].map(normalize_intent)
    grouped = dict(list(df.groupby(strata, sort=True)))
    quotas = _stratum_quotas({k: len(g) for k, g in grouped.items()}, max_rows)
    picked = [
        grouped[k].sample(n=quotas[k], random_state=seed)
        for k in sorted(grouped)
        if quotas[k] > 0
    ]
    return pd.concat(picked).sort_index().reset_index(drop=True)


def intent_mix(values: pd.Series) -> dict[str, float]:
    """Share of each intent bucket in a raw ``PRED_CATEGORY_WITH_SALES`` column.

    Used by the loader to show that the sampled batch kept the file's mix.
    """
    if values.empty:
        return {}
    counts = values.map(normalize_intent).value_counts(normalize=True)
    return {str(k): round(float(v), 4) for k, v in counts.items()}
