"""LSQ push stage: send finalized assignments to LeadSquared's bulk API.

Terminal stage of the nightly pipeline. It reads the run's pending assignments,
formats them as LSQ lead-update records, pushes them in batches, and records the
per-lead outcome in ``assignments.push_status``.

Prod vs hackathon
-----------------
Production points ``LSQ_API_BASE_URL`` at the real LeadSquared endpoint and
supplies the key from Secrets Manager; this stage then behaves exactly like the
existing production Lambda. In the hackathon the default base URL is a
non-routable ``*.local`` sentinel and outbound egress is deliberately off (no NAT
gateway), so a real call would only hang until timeout. When the endpoint is a
mock/sentinel the push is simulated - every record is marked pushed and the
formatted payload is logged - so the demo shows the full flow without needing a
live CRM. Swapping to prod is purely configuration: real URL + real key.

The payload builder is a pure function so the record format is unit-testable
without a network.
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd
import requests
from sqlalchemy import text

from shared.config import get_config
from shared.constants import PUSH_STATUS_FAILED, PUSH_STATUS_SUCCESS
from shared.db import get_engine, read_sql
from shared.pipeline import complete_run, fail_run, update_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# LSQ bulk endpoints cap the records per call; keep batches well under it.
PUSH_BATCH_SIZE = int(os.getenv("LSQ_BATCH_SIZE", "500"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("LSQ_HTTP_TIMEOUT", "30"))

_PENDING_ASSIGNMENTS = """
SELECT a.lead_id, a.primary_manager_id, a.fallback_manager_id,
       a.confidence_score, a.match_score, a.intent_bucket,
       mp.manager_name AS primary_manager_name
FROM assignments a
LEFT JOIN manager_profiles mp ON mp.manager_id = a.primary_manager_id
WHERE a.run_id = :run_id AND a.push_status = 'pending'
ORDER BY a.lead_id
"""


def _is_mock_endpoint(base_url: str) -> bool:
    """True when the configured endpoint is a local/mock sentinel, not a CRM."""
    if os.getenv("LSQ_MOCK", "").lower() in {"1", "true", "yes"}:
        return True
    url = (base_url or "").lower()
    return "mock" in url or url.startswith("https://localhost") or ".local" in url


def build_lsq_payload(assignments: pd.DataFrame) -> list[dict]:
    """Format assignment rows as LSQ lead-update records.

    Each record carries the lead's CRM id (``ProspectID``) and the fields LSQ
    should set: the assigned owner and a couple of custom attributes describing
    the routing decision. This mirrors the shape the production bulk update
    expects, kept in one place so a schema change is a single edit.
    """
    records = []
    for row in assignments.itertuples(index=False):
        records.append(
            {
                "ProspectID": row.lead_id,
                "LeadPropertyList": [
                    {"Attribute": "mx_Assigned_Manager", "Value": row.primary_manager_id},
                    {"Attribute": "mx_Assigned_Manager_Name",
                     "Value": row.primary_manager_name or row.primary_manager_id},
                    {"Attribute": "mx_Fallback_Manager", "Value": row.fallback_manager_id or ""},
                    {"Attribute": "mx_Assignment_Confidence",
                     "Value": f"{float(row.confidence_score):.4f}"},
                    {"Attribute": "mx_Intent_Bucket", "Value": row.intent_bucket or ""},
                ],
            }
        )
    return records


def _push_batch_real(records: list[dict], cfg) -> bool:
    """POST one batch to the LSQ bulk endpoint. Returns True on 2xx."""
    url = f"{cfg.lsq_api_base_url.rstrip('/')}/LeadManagement.svc/Leads.BulkUpdate"
    resp = requests.post(
        url,
        params={"accessKey": cfg.lsq_api_key},
        data=json.dumps(records),
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return True


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


def lambda_handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("lsq_push requires run_id in the event")

    try:
        cfg = get_config()
        mock = _is_mock_endpoint(cfg.lsq_api_base_url)

        assignments = read_sql(_PENDING_ASSIGNMENTS, {"run_id": run_id})
        total = int(len(assignments))
        pushed = 0
        failed = 0

        for start in range(0, total, PUSH_BATCH_SIZE):
            chunk = assignments.iloc[start : start + PUSH_BATCH_SIZE]
            lead_ids = chunk["lead_id"].tolist()
            records = build_lsq_payload(chunk)

            try:
                if mock:
                    logger.info(
                        "lsq_push run=%s MOCK batch=%s..%s records=%s (first=%s)",
                        run_id, start, start + len(chunk), len(records),
                        json.dumps(records[0]) if records else "{}",
                    )
                else:
                    _push_batch_real(records, cfg)
                _update_push_status(run_id, lead_ids, PUSH_STATUS_SUCCESS)
                pushed += len(lead_ids)
            except Exception:
                logger.exception("lsq_push batch failed run=%s start=%s", run_id, start)
                _update_push_status(run_id, lead_ids, PUSH_STATUS_FAILED)
                failed += len(lead_ids)

        update_run(run_id, stage="lsq_push")
        # Terminal stage: close out the run so the audit log reflects completion.
        complete_run(run_id)

        logger.info(
            "lsq_push run=%s mock=%s total=%s pushed=%s failed=%s",
            run_id, mock, total, pushed, failed,
        )

        return {
            **{k: event[k] for k in ("run_id", "batch_id", "business_date", "model_id") if k in event},
            "assignments_total": total,
            "pushed": pushed,
            "failed": failed,
            "mock": mock,
        }
    except Exception as exc:
        logger.exception("lsq_push failed for run %s", run_id)
        fail_run(run_id, str(exc), stage="lsq_push")
        raise
