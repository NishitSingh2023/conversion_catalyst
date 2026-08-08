"""Integration tests for the eligibility filter.

The filter is SQL because the candidate set is |leads| x |managers| - 3M rows at
the target scale - so these tests drive the real statements against Postgres
using an isolated batch and run id, then clean up after themselves.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from lambdas.eligibility import handler
from lambdas.eligibility.handler import lambda_handler as eligibility

LEAD_LANG, LEAD_GEO = "Hindi", "Delhi"

# manager_id -> (languages, geographies, active, load_today)
MANAGERS = {
    "T_OK":         ([LEAD_LANG], [LEAD_GEO], True, 0),
    "T_STALE":      ([LEAD_LANG], [LEAD_GEO], False, 0),
    "T_FULL":       ([LEAD_LANG], [LEAD_GEO], True, 50),
    "T_NEARLY":     ([LEAD_LANG], [LEAD_GEO], True, 49),
    "T_WRONG_LANG": (["Tamil"],   [LEAD_GEO], True, 0),
    "T_WRONG_GEO":  ([LEAD_LANG], ["Mumbai"], True, 0),
}


@pytest.fixture
def fixture_run(db):
    """Insert an isolated batch + managers, yield ids, then tear everything down."""
    batch_id = f"test-{uuid.uuid4().hex[:8]}"
    run_id = f"testrun-{uuid.uuid4().hex[:8]}"
    business_date = "2026-06-15"

    with db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO new_leads (lead_id, intent_bucket, geography, language,
                                       product_interest, lead_source, grade, batch_id, is_valid)
                VALUES (:lid, 'H', :geo, :lang, 'JEE', 'organic', '11', :batch, TRUE)
                """
            ),
            {"lid": f"{batch_id}-L1", "geo": LEAD_GEO, "lang": LEAD_LANG, "batch": batch_id},
        )
        # An invalid lead that every downstream stage must ignore.
        conn.execute(
            text(
                """
                INSERT INTO new_leads (lead_id, intent_bucket, geography, language,
                                       product_interest, batch_id, is_valid, validation_error)
                VALUES (:lid, 'X', :geo, :lang, 'JEE', :batch, FALSE, 'invalid_intent_bucket')
                """
            ),
            {"lid": f"{batch_id}-BAD", "geo": LEAD_GEO, "lang": LEAD_LANG, "batch": batch_id},
        )

        for mid, (langs, geos, active, load) in MANAGERS.items():
            conn.execute(
                text(
                    """
                    INSERT INTO manager_profiles (manager_id, languages_handled,
                        geographies_handled, products_handled, derived_active_flag)
                    VALUES (:mid, :langs, :geos, ARRAY['JEE'], :active)
                    ON CONFLICT (manager_id) DO UPDATE SET
                        languages_handled = EXCLUDED.languages_handled,
                        geographies_handled = EXCLUDED.geographies_handled,
                        derived_active_flag = EXCLUDED.derived_active_flag
                    """
                ),
                {"mid": mid, "langs": langs, "geos": geos, "active": active},
            )
            # Existing load is expressed as real assignments on the business date,
            # exercising the manager_daily_load view the optimizer also uses.
            for i in range(load):
                conn.execute(
                    text(
                        """
                        INSERT INTO assignments (run_id, lead_id, primary_manager_id,
                                                 business_date)
                        VALUES (:rid, :lid, :mid, :bd)
                        """
                    ),
                    {"rid": f"{run_id}-seed", "lid": f"{mid}-seed-{i}", "mid": mid, "bd": business_date},
                )

        conn.execute(
            text("INSERT INTO pipeline_runs (run_id, batch_id, status) VALUES (:r, :b, 'running')"),
            {"r": run_id, "b": batch_id},
        )

    yield {"run_id": run_id, "batch_id": batch_id, "business_date": business_date}

    with db.begin() as conn:
        for stmt, params in [
            ("DELETE FROM eligibility_matrix WHERE run_id = :r", {"r": run_id}),
            ("DELETE FROM assignments WHERE run_id = :r", {"r": f"{run_id}-seed"}),
            ("DELETE FROM pipeline_runs WHERE run_id = :r", {"r": run_id}),
            ("DELETE FROM new_leads WHERE batch_id = :b", {"b": batch_id}),
            ("DELETE FROM manager_profiles WHERE manager_id LIKE 'T\\_%'", {}),
        ]:
            conn.execute(text(stmt), params)


def _matrix(db, run_id) -> dict[str, object]:
    rows = db.connect().execute(
        text(
            "SELECT manager_id, eligible, rejection_reason FROM eligibility_matrix "
            "WHERE run_id = :r"
        ),
        {"r": run_id},
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_only_fully_capable_managers_are_eligible(db, fixture_run):
    eligibility(fixture_run)
    m = _matrix(db, fixture_run["run_id"])
    assert m["T_OK"][0] is True
    assert m["T_NEARLY"][0] is True, "49 leads is under the cap"
    for blocked in ("T_STALE", "T_FULL", "T_WRONG_LANG", "T_WRONG_GEO"):
        assert m[blocked][0] is False, f"{blocked} should be filtered out"


def test_rejection_reasons_are_specific(db, fixture_run):
    eligibility(fixture_run)
    m = _matrix(db, fixture_run["run_id"])
    assert m["T_STALE"][1] == "inactive_no_recent_activity"
    assert m["T_FULL"][1] == "at_capacity"
    assert m["T_WRONG_LANG"][1] == "language_mismatch"
    assert m["T_WRONG_GEO"][1] == "geography_mismatch"
    assert m["T_OK"][1] is None


def test_capacity_boundary_at_the_cap(db, fixture_run):
    """50 held leads blocks, 49 does not - the cap is inclusive."""
    eligibility(fixture_run)
    m = _matrix(db, fixture_run["run_id"])
    assert m["T_FULL"][1] == "at_capacity"
    assert m["T_NEARLY"][0] is True


def test_invalid_leads_are_excluded_entirely(db, fixture_run):
    """A lead flagged invalid at ingest must not appear for any manager."""
    eligibility(fixture_run)
    rows = db.connect().execute(
        text("SELECT count(*) FROM eligibility_matrix WHERE run_id = :r AND lead_id LIKE '%-BAD'"),
        {"r": fixture_run["run_id"]},
    ).scalar()
    assert rows == 0


def test_counts_returned_not_lead_lists(db, fixture_run):
    """Step Functions caps state at 256KB, so the payload must stay bounded."""
    result = eligibility(fixture_run)
    assert result["leads_valid"] == 1
    assert result["leads_with_candidates"] == 1
    assert result["unassignable_leads"] == 0
    assert result["eligible_pairs"] == 2  # T_OK and T_NEARLY
    for value in result.values():
        assert not isinstance(value, list), "no unbounded lists in the payload"


def test_rerun_is_idempotent(db, fixture_run):
    """Re-running a run_id replaces its rows rather than duplicating them."""
    first = eligibility(fixture_run)
    second = eligibility(fixture_run)
    assert first["eligible_pairs"] == second["eligible_pairs"]
    total = db.connect().execute(
        text("SELECT count(*) FROM eligibility_matrix WHERE run_id = :r"),
        {"r": fixture_run["run_id"]},
    ).scalar()
    assert total == len(MANAGERS), "one row per manager for the single valid lead"


def test_shortlist_caps_eligible_pairs_per_lead(db, fixture_run, monkeypatch):
    """The per-lead shortlist bounds how many eligible pairs are written.

    On the real dataset ~433 managers pass the filters for a typical lead, which
    would put 11.6M rows in this table per run. Only the strongest N survive.
    """
    monkeypatch.setattr(handler, "ELIGIBLE_MANAGERS_PER_LEAD", 1)
    result = eligibility(fixture_run)

    assert result["eligible_pairs"] == 1, "two managers qualify, only one is kept"
    assert result["eligible_managers_per_lead"] == 1
    # A capped lead still counts as having candidates, so it is not reported
    # unassignable and the pool will call it capacity_overflow, not
    # no_eligible_manager.
    assert result["leads_with_candidates"] == 1
    assert result["unassignable_leads"] == 0


def test_shortlist_keeps_the_highest_converting_managers(db, fixture_run, monkeypatch):
    """Ranking is by the manager's conversion rate, descending."""
    with db.begin() as conn:
        conn.execute(
            text("UPDATE manager_profiles SET conv_rate_overall = 0.9 WHERE manager_id = 'T_NEARLY'"),
        )
        conn.execute(
            text("UPDATE manager_profiles SET conv_rate_overall = 0.1 WHERE manager_id = 'T_OK'"),
        )

    monkeypatch.setattr(handler, "ELIGIBLE_MANAGERS_PER_LEAD", 1)
    eligibility(fixture_run)

    kept = db.connect().execute(
        text("SELECT manager_id FROM eligibility_matrix WHERE run_id = :r AND eligible"),
        {"r": fixture_run["run_id"]},
    ).scalars().all()
    assert kept == ["T_NEARLY"]


def test_capped_out_manager_is_explained_not_dropped(db, fixture_run, monkeypatch):
    """An eligible manager below the cap is recorded, distinct from a rule failure.

    The rejection sample exists to answer "why was this rep not offered my
    lead?"; a rep silently absent from the table cannot answer it.
    """
    monkeypatch.setattr(handler, "ELIGIBLE_MANAGERS_PER_LEAD", 1)
    eligibility(fixture_run)
    m = _matrix(db, fixture_run["run_id"])

    # Every manager is still accounted for, none dropped.
    assert set(m) == set(MANAGERS)
    excluded = [mid for mid, (ok, _) in m.items() if not ok]
    assert len(excluded) == len(MANAGERS) - 1
    not_shortlisted = [mid for mid, (ok, reason) in m.items() if reason == "not_shortlisted"]
    assert len(not_shortlisted) == 1, "the runner-up eligible manager, ranked out"
    # Rule failures keep their specific reasons rather than becoming 'not_shortlisted'.
    assert m["T_STALE"][1] == "inactive_no_recent_activity"
    assert m["T_WRONG_LANG"][1] == "language_mismatch"


def test_shortlists_spread_across_the_roster(db, monkeypatch):
    """Different leads must get different shortlists, or the cap becomes the capacity.

    ``conv_rate_overall`` is a property of the manager, not of the pair, so
    ranking on it alone hands every lead in a candidate cohort the same managers.
    Measured on the real batch that put 196 of 902 active managers on any
    shortlist, capping usable capacity at 196 x 50 seats: 9,070 leads were
    assigned and 17,736 overflowed to the pool while 700 managers sat idle.

    This drives the two orderings against the same fixture: ranking purely by
    conversion rate reaches only as many managers as the shortlist is deep, while
    reserving part of the shortlist for a spread sample reaches far more.
    """
    batch_id = f"test-{uuid.uuid4().hex[:8]}"
    # Enough leads that the spread sample reaching every manager is a near
    # certainty rather than a coin toss: one slot drawn from 8 managers over 120
    # leads misses a given manager with probability (7/8)^120, about 1 in 10^7.
    n_leads, n_managers, depth = 120, 8, 2
    managers = [f"T_D{i}" for i in range(n_managers)]

    with db.begin() as conn:
        for i in range(n_leads):
            conn.execute(
                text(
                    """
                    INSERT INTO new_leads (lead_id, intent_bucket, geography, language,
                                           product_interest, batch_id, is_valid)
                    VALUES (:lid, 'H', :geo, :lang, 'JEE', :batch, TRUE)
                    """
                ),
                {"lid": f"{batch_id}-L{i}", "geo": LEAD_GEO, "lang": LEAD_LANG,
                 "batch": batch_id},
            )
        # Distinct conversion rates, so the quality ranking is a strict order and
        # the same top managers win for every lead.
        for rank, manager_id in enumerate(managers):
            conn.execute(
                text(
                    """
                    INSERT INTO manager_profiles (manager_id, languages_handled,
                        geographies_handled, products_handled, derived_active_flag,
                        conv_rate_overall)
                    VALUES (:mid, ARRAY[:lang], ARRAY[:geo], ARRAY['JEE'], TRUE, :rate)
                    """
                ),
                {"mid": manager_id, "lang": LEAD_LANG, "geo": LEAD_GEO,
                 "rate": 0.9 - rank * 0.1},
            )

    def run_with(quality_slots: int) -> dict:
        run_id = f"testrun-{uuid.uuid4().hex[:8]}"
        with db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_runs (run_id, batch_id, status) "
                    "VALUES (:r, :b, 'running')"
                ),
                {"r": run_id, "b": batch_id},
            )
        monkeypatch.setattr(handler, "ELIGIBLE_MANAGERS_PER_LEAD", depth)
        monkeypatch.setattr(handler, "ELIGIBLE_TOP_BY_CONV_RATE", quality_slots)
        result = eligibility(
            {"run_id": run_id, "batch_id": batch_id, "business_date": "2026-06-15"}
        )
        with db.begin() as conn:
            conn.execute(
                text("DELETE FROM eligibility_matrix WHERE run_id = :r"), {"r": run_id}
            )
            conn.execute(text("DELETE FROM pipeline_runs WHERE run_id = :r"), {"r": run_id})
        return result

    try:
        quality_only = run_with(quality_slots=depth)
        stratified = run_with(quality_slots=1)

        # Pure quality ranking cannot reach past the shortlist depth, however many
        # managers are eligible and free.
        assert quality_only["managers_shortlisted"] == depth
        # Reserving a slot for the spread sample reaches the rest of the roster.
        assert stratified["managers_shortlisted"] > depth
        assert stratified["managers_shortlisted"] == n_managers

        # Both still respect the cap, and every lead still gets a full shortlist.
        for result in (quality_only, stratified):
            assert result["eligible_pairs"] <= n_leads * depth
            assert result["leads_with_candidates"] == n_leads
    finally:
        with db.begin() as conn:
            conn.execute(text("DELETE FROM new_leads WHERE batch_id = :b"), {"b": batch_id})
            conn.execute(
                text("DELETE FROM manager_profiles WHERE manager_id = ANY(:ids)"),
                {"ids": managers},
            )


def test_quality_slice_cannot_exceed_the_shortlist(db, fixture_run, monkeypatch):
    """A quality slice wider than the shortlist must clamp, not disable the spread."""
    monkeypatch.setattr(handler, "ELIGIBLE_MANAGERS_PER_LEAD", 1)
    monkeypatch.setattr(handler, "ELIGIBLE_TOP_BY_CONV_RATE", 999)

    result = eligibility(fixture_run)
    assert result["eligible_pairs"] == 1, "the cap still holds"


def test_missing_run_id_raises(db):
    with pytest.raises(ValueError, match="run_id"):
        eligibility({"batch_id": "whatever"})


def test_missing_batch_id_fails_rather_than_reporting_empty_success(db, fixture_run):
    """An absent batch matched no rows and looked like a successful empty run."""
    with pytest.raises(ValueError, match="batch_id"):
        eligibility({"run_id": fixture_run["run_id"]})
