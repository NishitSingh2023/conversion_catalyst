"""Business constants shared across the pipeline.

Keeping these in one module means the 50-lead cap, the intent ordering, and the
feature list are defined once and reused by eligibility, optimizer, training, and
the dashboard.
"""
from __future__ import annotations

# --- Workload -------------------------------------------------------------
# Maximum number of leads auto-assigned to a single manager per nightly run.
# Anything beyond this cap for an otherwise-best manager overflows to the pool.
MAX_LEADS_PER_MANAGER: int = 50

# A manager is considered "active" (available) if they have had at least one
# interaction in the trailing window below. Because there is no live availability
# feed in the hackathon dataset, activity is derived from lead_manager_history.
ACTIVE_WINDOW_DAYS: int = 30

# --- Intent buckets -------------------------------------------------------
# The upstream HML model tags every lead with one of these buckets.
INTENT_BUCKETS: tuple[str, ...] = ("H", "M", "L", "EL")

# Lower rank = higher priority. Used to order the pool and to prioritise which
# leads get first pick of the strongest managers in the optimizer.
INTENT_PRIORITY: dict[str, int] = {"H": 0, "M": 1, "L": 2, "EL": 3}

# --- Pool statuses --------------------------------------------------------
POOL_STATUS_AVAILABLE = "available"
POOL_STATUS_CLAIMED = "claimed"

# --- Assignment push statuses --------------------------------------------
PUSH_STATUS_PENDING = "pending"
PUSH_STATUS_SUCCESS = "success"
PUSH_STATUS_FAILED = "failed"

# --- Pipeline run statuses -----------------------------------------------
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILED = "failed"

# --- Feature engineering --------------------------------------------------
# Categorical lead attributes that get one-hot encoded. Note: parent_student is
# intentionally excluded because it is not present in lead_manager_history; using
# it would create train/serve skew (always "unknown" at train time). It is still
# stored on new_leads for reporting.
LEAD_CATEGORICAL_FEATURES: tuple[str, ...] = (
    "intent_bucket",
    "geography",
    "language",
    "product_interest",
    "lead_source",
    "grade",
)

# Derived manager attributes joined onto each (lead, manager) pair.
MANAGER_NUMERIC_FEATURES: tuple[str, ...] = (
    "conv_rate_overall",
    "conv_rate_H",
    "conv_rate_M",
    "conv_rate_L",
    "avg_response_mins",
    "total_leads_handled",
)

# Interaction / match features computed between a lead and a manager profile.
MATCH_FEATURES: tuple[str, ...] = (
    "language_match",
    "geography_match",
    "product_overlap",
)

# The model's binary target column in lead_manager_history.
TARGET_COLUMN: str = "converted"
