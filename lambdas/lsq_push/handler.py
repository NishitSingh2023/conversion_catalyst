"""LSQ push stage: send finalized assignments to LeadSquared's bulk update API.

Terminal stage of the nightly pipeline. It reads the run's pending assignments,
groups them by assigned manager, pushes one bulk-update call per manager group,
and records the per-lead outcome in ``assignments.push_status``.

The API contract (LeadSquared "Update Leads in Bulk")
-----------------------------------------------------
``POST {host}/v2/LeadManagement.svc/Lead/Bulk/Update?accessKey=..&secretKey=..``

with a body of::

    {"LeadIds": ["id", ...],
     "LeadFields": [{"Attribute": "<schema_name>", "Value": "<value>"}]}

The endpoint applies the SAME field values to EVERY listed lead id, accepts at
most 50 lead ids per call, and rate limits at 25 calls / 5 seconds (HTTP 429
beyond that). Success looks like
``{"Status": "Success", "Message": {"AffectedRows": N}}``; 401 means bad
credentials and 400 a malformed body.

Because one call sets one set of values, assignments are grouped by
``primary_manager_id`` and chunked to 50 ids - 471 leads across 21 managers is
~30 calls rather than one call per lead.

Safe by default
---------------
A misconfigured base URL must never be able to start writing to a live CRM, so
this stage simulates the push unless ``LSQ_LIVE_PUSH`` is explicitly set. It
does not try to infer intent from the shape of the URL: the previous sniffing
approach ("does the host look like a mock?") meant that merely pointing
``LSQ_API_BASE_URL`` at the real host silently began mutating CRM records.
Simulated runs mark every record pushed and log the exact body that would have
been sent, so the demo shows the full flow with no live CRM.

``build_lsq_payload`` is a pure function, so the request shape is unit-testable
without a network.
"""
from __future__ import annotations

import json
import logging
import os
import time

import pandas as pd
import requests
from sqlalchemy import text

from shared.config import get_config
from shared.constants import PUSH_STATUS_FAILED, PUSH_STATUS_SUCCESS
from shared.db import get_engine, read_sql
from shared.pipeline import complete_run, fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Hard API limit: the bulk endpoint rejects a call carrying more than 50 ids.
LSQ_MAX_LEADS_PER_CALL = 50

# Documented rate limit is 25 calls / 5 seconds. Pacing calls 0.2s apart keeps
# us at that ceiling without tracking a sliding window.
MIN_SECONDS_BETWEEN_CALLS = 5.0 / 25
# Pause before the single retry of a throttled call.
RATE_LIMIT_RETRY_SLEEP_SECONDS = 5.0

HTTP_TIMEOUT_SECONDS = int(os.getenv("LSQ_HTTP_TIMEOUT", "30"))

# The lead field that holds the owning sales rep. Its schema name is
# account-specific, so it is configurable and MUST be confirmed against the
# target LSQ account before any live push.
OWNER_ATTRIBUTE = os.getenv("LSQ_OWNER_ATTRIBUTE", "OwnerId")

_PENDING_ASSIGNMENTS = """
SELECT a.lead_id, a.primary_manager_id,
       mp.manager_name AS primary_manager_name
FROM assignments a
LEFT JOIN manager_profiles mp ON mp.manager_id = a.primary_manager_id
WHERE a.run_id = :run_id AND a.push_status = 'pending'
ORDER BY a.primary_manager_id, a.lead_id
"""


def resolve_batch_size(raw: str | None = None) -> int:
    """Leads per bulk call, clamped to the API maximum.

    ``LSQ_BATCH_SIZE`` can lower the batch for testing but cannot raise it above
    :data:`LSQ_MAX_LEADS_PER_CALL` - the API would reject the call outright, so
    honouring a larger override would only produce guaranteed failures.
    """
    try:
        requested = int(raw) if raw not in (None, "") else LSQ_MAX_LEADS_PER_CALL
    except ValueError:
        requested = LSQ_MAX_LEADS_PER_CALL
    return max(1, min(requested, LSQ_MAX_LEADS_PER_CALL))


PUSH_BATCH_SIZE = resolve_batch_size(os.getenv("LSQ_BATCH_SIZE"))


def is_live_push() -> bool:
    """True only when a live push has been explicitly opted into.

    Default is simulation. Writing to a CRM is irreversible, so it takes a
    deliberate ``LSQ_LIVE_PUSH=1`` rather than falling out of how the base URL
    happens to be spelled. ``LSQ_MOCK`` still forces simulation and wins, so an
    environment that pins it stays safe even if the opt-in is also set.
    """
    truthy = {"1", "true", "yes"}
    if os.getenv("LSQ_MOCK", "").lower() in truthy:
        return False
    return os.getenv("LSQ_LIVE_PUSH", "").lower() in truthy


def build_bulk_update_url(base_url: str) -> str:
    """Build the bulk-update URL, tolerating a host with or without ``/v2``.

    The real base URL is ``https://api-in21.leadsquared.com/v2/`` but the
    configured value may omit the version, so ``/v2`` is appended only when it
    is absent - blind concatenation would yield ``/v2/v2/`` and a 404.
    """
    base = (base_url or "").rstrip("/")
    if not base.lower().endswith("/v2"):
        base = f"{base}/v2"
    return f"{base}/LeadManagement.svc/Lead/Bulk/Update"


def build_lsq_payload(
    manager_id: str,
    lead_ids: list[str],
    manager_name: str | None = None,
    owner_attribute: str = OWNER_ATTRIBUTE,
) -> dict:
    """Build one bulk-update body assigning ``lead_ids`` to ``manager_id``.

    The endpoint applies identical field values to every listed lead, so a
    request body maps to exactly one (manager, lead ids) group. Per-lead values -
    ``confidence_score``, ``match_score``, ``fallback_manager_id``,
    ``intent_bucket`` - cannot ride along in a grouped call and are deliberately
    not pushed. Nothing is lost: they stay on the ``assignments`` row in Postgres
    and are shown per lead on the dashboard.

    Two caveats that matter before a live push:

    * ``owner_attribute`` is the schema name of the lead's owner field, which
      differs per LSQ account. The ``OwnerId`` default is a guess and MUST be
      confirmed against the target account's schema.
    * ``manager_id`` is our internal rep id. When leads were loaded with
      ``ANONYMIZE_REAL_DATA`` on, it is a pseudonymised id and NOT a real LSQ
      user id, so a live push would set the owner field to a value LSQ cannot
      resolve. Live use requires mapping back to genuine LSQ user ids first.
    """
    return {
        "LeadIds": list(lead_ids),
        "LeadFields": [
            {"Attribute": owner_attribute, "Value": str(manager_id)},
            {"Attribute": "mx_Assigned_Manager", "Value": str(manager_id)},
            {
                "Attribute": "mx_Assigned_Manager_Name",
                "Value": str(manager_name or manager_id),
            },
        ],
    }


class _RateLimiter:
    """Spaces successive calls by a minimum interval."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()


def _post_bulk_update(url: str, body: dict, cfg) -> requests.Response:
    return requests.post(
        url,
        # LSQ authenticates with BOTH keys as query parameters.
        params={"accessKey": cfg.lsq_api_key, "secretKey": cfg.lsq_secret_key},
        data=json.dumps(body),
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )


def _push_group_real(body: dict, cfg, limiter: _RateLimiter) -> int:
    """POST one group to the bulk endpoint. Returns AffectedRows.

    Retries exactly once on HTTP 429 after a short pause; anything else (401 bad
    credentials, 400 malformed body) is raised for the caller to record.
    """
    url = build_bulk_update_url(cfg.lsq_api_base_url)

    limiter.wait()
    resp = _post_bulk_update(url, body, cfg)
    if resp.status_code == 429:
        logger.warning("lsq_push throttled (429); retrying once in %ss",
                       RATE_LIMIT_RETRY_SLEEP_SECONDS)
        time.sleep(RATE_LIMIT_RETRY_SLEEP_SECONDS)
        limiter.wait()
        resp = _post_bulk_update(url, body, cfg)

    resp.raise_for_status()

    payload = resp.json()
    if str(payload.get("Status", "")).lower() != "success":
        raise RuntimeError(f"LSQ rejected the bulk update: {payload}")
    return int((payload.get("Message") or {}).get("AffectedRows", 0))


def _update_push_status(run_id: str, lead_ids: list[str], status: str) -> None:
    if not lead_ids:
        return
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE assignments SET push_status = :status "
                "WHERE run_id = :run_id AND lead_id = ANY(:ids)"
            ),
            {"status": status, "run_id": run_id, "ids": lead_ids},
        )


def _manager_groups(assignments: pd.DataFrame) -> list[tuple[str, str | None, list[str]]]:
    """Split assignments into ``(manager_id, manager_name, lead_ids)`` groups."""
    groups = []
    for manager_id, grp in assignments.groupby("primary_manager_id", sort=True):
        name = grp["primary_manager_name"].iloc[0]
        groups.append(
            (
                str(manager_id),
                None if pd.isna(name) else str(name),
                grp["lead_id"].tolist(),
            )
        )
    return groups


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("lsq_push requires run_id in the event")

    try:
        cfg = get_config()
        live = is_live_push()
        limiter = _RateLimiter(MIN_SECONDS_BETWEEN_CALLS)

        assignments = read_sql(_PENDING_ASSIGNMENTS, {"run_id": run_id})
        total = int(len(assignments))
        pushed = 0
        failed = 0
        calls = 0

        for manager_id, manager_name, manager_lead_ids in _manager_groups(assignments):
            for start in range(0, len(manager_lead_ids), PUSH_BATCH_SIZE):
                lead_ids = manager_lead_ids[start : start + PUSH_BATCH_SIZE]
                body = build_lsq_payload(manager_id, lead_ids, manager_name)
                calls += 1

                try:
                    if live:
                        _push_group_real(body, cfg, limiter)
                    else:
                        logger.info(
                            "lsq_push run=%s SIMULATED manager=%s leads=%s body=%s",
                            run_id, manager_id, len(lead_ids), json.dumps(body),
                        )
                    _update_push_status(run_id, lead_ids, PUSH_STATUS_SUCCESS)
                    pushed += len(lead_ids)
                except Exception:
                    logger.exception(
                        "lsq_push group failed run=%s manager=%s leads=%s",
                        run_id, manager_id, len(lead_ids),
                    )
                    _update_push_status(run_id, lead_ids, PUSH_STATUS_FAILED)
                    failed += len(lead_ids)

        update_run(run_id, stage="lsq_push")
        # Terminal stage: close out the run so the audit log reflects completion.
        complete_run(run_id)

        logger.info(
            "lsq_push run=%s live=%s total=%s pushed=%s failed=%s calls=%s",
            run_id, live, total, pushed, failed, calls,
        )

        return {
            **{k: event[k] for k in ("run_id", "batch_id", "business_date", "model_id") if k in event},
            "assignments_total": total,
            "pushed": pushed,
            "failed": failed,
            "api_calls": calls,
            "mock": not live,
        }
    except Exception as exc:
        logger.exception("lsq_push failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="lsq_push")
        raise
