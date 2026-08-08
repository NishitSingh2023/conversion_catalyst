"""Tests for the demo-only business-date capacity reset.

The reset deletes real output rows, so the two things worth pinning are that it
never fires unless the event asks for it, and that it cannot reach another
business date's data.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from lambdas.ingest import handler as ingest

TARGET_DATE = "2031-03-01"
OTHER_DATE = "2031-03-02"


def _stub_run(monkeypatch, calls: list[str]) -> None:
    """Neutralise every DB-touching part of ingest except the reset guard."""
    monkeypatch.setattr(ingest, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "update_run", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "refresh_manager_profiles", lambda: 0)
    monkeypatch.setattr(
        ingest, "validate_batch",
        lambda batch_id: {"total": 1, "valid": 1, "invalid": 0, "invalid_reasons": {}},
    )
    monkeypatch.setattr(
        ingest, "reset_business_date",
        lambda business_date: calls.append(business_date) or {"assignments_deleted": 0,
                                                              "pool_deleted": 0},
    )


def test_reset_is_off_by_default(monkeypatch):
    calls: list[str] = []
    _stub_run(monkeypatch, calls)

    for event in (
        {"batch_id": "b1", "business_date": TARGET_DATE},
        {"batch_id": "b1", "business_date": TARGET_DATE, "reset_business_date": False},
        # Only an explicit boolean true opts in; a truthy string does not.
        {"batch_id": "b1", "business_date": TARGET_DATE, "reset_business_date": "yes"},
    ):
        ingest.lambda_handler(event)

    assert calls == []


def test_reset_runs_when_explicitly_requested(monkeypatch):
    calls: list[str] = []
    _stub_run(monkeypatch, calls)

    out = ingest.lambda_handler(
        {"batch_id": "b1", "business_date": TARGET_DATE, "reset_business_date": True}
    )

    assert calls == [TARGET_DATE]
    assert out["reset"] == {"assignments_deleted": 0, "pool_deleted": 0}


@pytest.mark.usefixtures("db")
def test_reset_only_touches_the_given_business_date(db):
    with db.begin() as conn:
        conn.execute(
            text("DELETE FROM assignments WHERE business_date IN (:d1, :d2)"),
            {"d1": TARGET_DATE, "d2": OTHER_DATE},
        )
        conn.execute(text("DELETE FROM pool WHERE run_id IN ('reset-target', 'reset-other')"))
        conn.execute(
            text(
                """
                INSERT INTO assignments
                    (run_id, lead_id, primary_manager_id, business_date)
                VALUES ('reset-target', 'L1', 'MGR1', :d1),
                       ('reset-other',  'L2', 'MGR1', :d2)
                """
            ),
            {"d1": TARGET_DATE, "d2": OTHER_DATE},
        )
        conn.execute(
            text(
                """
                INSERT INTO pool
                    (run_id, lead_id, intent_bucket, priority_rank, status, claimed_by, claimed_at)
                VALUES ('reset-target', 'L3', 'H', 1, 'available', NULL, NULL),
                       ('reset-other',  'L4', 'H', 1, 'claimed',   'MGR1', :ts2)
                """
            ),
            {"ts2": f"{OTHER_DATE} 09:00:00+00"},
        )

    stats = ingest.reset_business_date(TARGET_DATE)

    assert stats["assignments_deleted"] == 1
    # The target run's pool row goes via its run's business_date.
    assert stats["pool_deleted"] == 1

    with db.connect() as conn:
        surviving_assignments = conn.execute(
            text("SELECT lead_id FROM assignments WHERE run_id IN ('reset-target', 'reset-other')")
        ).scalars().all()
        surviving_pool = conn.execute(
            text("SELECT lead_id FROM pool WHERE run_id IN ('reset-target', 'reset-other')")
        ).scalars().all()

    assert surviving_assignments == ["L2"]
    assert surviving_pool == ["L4"]

    with db.begin() as conn:
        conn.execute(text("DELETE FROM assignments WHERE run_id = 'reset-other'"))
        conn.execute(text("DELETE FROM pool WHERE run_id = 'reset-other'"))
