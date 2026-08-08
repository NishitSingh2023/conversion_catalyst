"""Contract tests for the LSQ bulk-update push.

The bulk endpoint applies the same field values to every lead id in the call, so
the request shape is not a detail - getting it wrong means the CRM is either not
updated or updated for the wrong leads. These tests pin the documented contract:
body shape, the 50-id ceiling, URL construction, and the requirement that a live
push be explicitly opted into.
"""
from __future__ import annotations

import pandas as pd

from lambdas.lsq_push import handler as lsq


def test_payload_matches_documented_body_shape():
    body = lsq.build_lsq_payload("MGR0001", ["L1", "L2"], manager_name="Asha")

    assert set(body) == {"LeadIds", "LeadFields"}
    assert body["LeadIds"] == ["L1", "L2"]
    # Every field is an {Attribute, Value} pair of strings.
    for field in body["LeadFields"]:
        assert set(field) == {"Attribute", "Value"}
        assert isinstance(field["Attribute"], str)
        assert isinstance(field["Value"], str)

    by_attr = {f["Attribute"]: f["Value"] for f in body["LeadFields"]}
    assert by_attr[lsq.OWNER_ATTRIBUTE] == "MGR0001"
    assert by_attr["mx_Assigned_Manager_Name"] == "Asha"


def test_payload_carries_no_per_lead_fields():
    """A grouped call cannot hold per-lead values, so none may leak in."""
    body = lsq.build_lsq_payload("MGR0001", ["L1", "L2"])

    attrs = {f["Attribute"] for f in body["LeadFields"]}
    assert not attrs & {
        "mx_Assignment_Confidence", "mx_Intent_Bucket", "mx_Fallback_Manager",
    }
    # Missing manager name falls back to the id rather than "None".
    by_attr = {f["Attribute"]: f["Value"] for f in body["LeadFields"]}
    assert by_attr["mx_Assigned_Manager_Name"] == "MGR0001"


def test_owner_attribute_is_configurable():
    body = lsq.build_lsq_payload("MGR0001", ["L1"], owner_attribute="mx_Owner")
    assert body["LeadFields"][0] == {"Attribute": "mx_Owner", "Value": "MGR0001"}


def test_batch_size_clamped_to_api_maximum():
    assert lsq.resolve_batch_size(None) == 50
    assert lsq.resolve_batch_size("500") == 50      # env override cannot exceed the cap
    assert lsq.resolve_batch_size("10") == 10       # lowering is allowed
    assert lsq.resolve_batch_size("0") == 1         # never a zero-sized batch
    assert lsq.resolve_batch_size("not-a-number") == 50
    assert lsq.PUSH_BATCH_SIZE <= lsq.LSQ_MAX_LEADS_PER_CALL


def test_no_group_exceeds_the_id_limit():
    """120 leads on one manager must split into calls of at most 50 ids."""
    assignments = pd.DataFrame(
        {
            "lead_id": [f"L{i}" for i in range(120)],
            "primary_manager_id": ["MGR0001"] * 120,
            "primary_manager_name": ["Asha"] * 120,
        }
    )

    groups = lsq._manager_groups(assignments)
    assert len(groups) == 1

    bodies = []
    for manager_id, name, lead_ids in groups:
        for start in range(0, len(lead_ids), lsq.PUSH_BATCH_SIZE):
            bodies.append(
                lsq.build_lsq_payload(manager_id, lead_ids[start : start + lsq.PUSH_BATCH_SIZE], name)
            )

    assert len(bodies) == 3
    assert all(len(b["LeadIds"]) <= lsq.LSQ_MAX_LEADS_PER_CALL for b in bodies)
    # Every lead is pushed exactly once.
    pushed = [lid for b in bodies for lid in b["LeadIds"]]
    assert sorted(pushed) == sorted(assignments["lead_id"])


def test_manager_groups_splits_by_manager():
    assignments = pd.DataFrame(
        {
            "lead_id": ["L1", "L2", "L3"],
            "primary_manager_id": ["MGR0001", "MGR0002", "MGR0001"],
            "primary_manager_name": ["Asha", None, "Asha"],
        }
    )

    groups = dict((m, ids) for m, _name, ids in lsq._manager_groups(assignments))
    assert groups == {"MGR0001": ["L1", "L3"], "MGR0002": ["L2"]}


def test_url_never_doubles_the_version_segment():
    expected = "/v2/LeadManagement.svc/Lead/Bulk/Update"
    assert lsq.build_bulk_update_url("https://api-in21.leadsquared.com/v2/") == (
        "https://api-in21.leadsquared.com" + expected
    )
    assert lsq.build_bulk_update_url("https://api-in21.leadsquared.com") == (
        "https://api-in21.leadsquared.com" + expected
    )
    assert lsq.build_bulk_update_url("https://api-in21.leadsquared.com/v2").count("/v2") == 1


def test_live_push_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("LSQ_MOCK", raising=False)
    monkeypatch.delenv("LSQ_LIVE_PUSH", raising=False)
    assert lsq.is_live_push() is False

    # A real-looking URL alone must not enable live writes.
    monkeypatch.setenv("LSQ_API_BASE_URL", "https://api-in21.leadsquared.com/v2/")
    assert lsq.is_live_push() is False

    monkeypatch.setenv("LSQ_LIVE_PUSH", "1")
    assert lsq.is_live_push() is True

    # LSQ_MOCK still forces simulation and wins over the opt-in.
    monkeypatch.setenv("LSQ_MOCK", "true")
    assert lsq.is_live_push() is False
