-- =====================================================================
-- Lead Assignment Engine - core schema
-- =====================================================================
-- Design notes:
--   * lead_manager_history is the single source of truth. It holds the
--     (lead, manager, converted?) triples used both to TRAIN the model and to
--     DERIVE every manager attribute (there is no separate managers table).
--   * new_leads holds the batch to be assigned on a given run.
--   * manager_profiles is a MATERIALISED derivation of lead_manager_history,
--     rebuilt at the start of each run.
--   * eligibility_matrix / scores are per-run intermediate tables kept for
--     debuggability and explainability.
--   * assignments / pool are the run outputs; pipeline_runs is the audit log.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Input: historical (lead, manager, converted?) triples
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead_manager_history (
    id                 BIGSERIAL PRIMARY KEY,
    lead_id            TEXT        NOT NULL,
    manager_id         TEXT        NOT NULL,
    lead_intent_bucket TEXT        NOT NULL,          -- H / M / L / EL
    lead_geography     TEXT,
    lead_language      TEXT,
    lead_product       TEXT,
    lead_source        TEXT,
    lead_grade         TEXT,
    contact_attempts   INTEGER     DEFAULT 0,
    first_response_mins DOUBLE PRECISION,
    converted          BOOLEAN     NOT NULL,          -- model target
    interaction_date   DATE        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lmh_manager   ON lead_manager_history (manager_id);
CREATE INDEX IF NOT EXISTS idx_lmh_intent    ON lead_manager_history (lead_intent_bucket);
CREATE INDEX IF NOT EXISTS idx_lmh_date      ON lead_manager_history (interaction_date);

-- ---------------------------------------------------------------------
-- Input: the batch of new, pre-classified leads to assign
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS new_leads (
    lead_id          TEXT PRIMARY KEY,
    intent_bucket    TEXT NOT NULL,                   -- H / M / L / EL
    geography        TEXT,
    language         TEXT,
    product_interest TEXT,
    lead_source      TEXT,
    grade            TEXT,
    parent_student   TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    batch_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_new_leads_batch ON new_leads (batch_id);

-- ---------------------------------------------------------------------
-- Derived: manager profiles (materialised from lead_manager_history)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manager_profiles (
    manager_id          TEXT PRIMARY KEY,
    languages_handled   TEXT[]  NOT NULL DEFAULT '{}',
    geographies_handled TEXT[]  NOT NULL DEFAULT '{}',
    products_handled    TEXT[]  NOT NULL DEFAULT '{}',
    conv_rate_overall   DOUBLE PRECISION DEFAULT 0,
    conv_rate_H         DOUBLE PRECISION DEFAULT 0,
    conv_rate_M         DOUBLE PRECISION DEFAULT 0,
    conv_rate_L         DOUBLE PRECISION DEFAULT 0,
    avg_response_mins   DOUBLE PRECISION,
    total_leads_handled INTEGER DEFAULT 0,
    last_active_date    DATE,
    derived_active_flag BOOLEAN DEFAULT FALSE,
    refreshed_at        TIMESTAMPTZ DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Model registry
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_registry (
    model_id      TEXT PRIMARY KEY,
    trained_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    s3_path       TEXT,
    auc           DOUBLE PRECISION,
    precision     DOUBLE PRECISION,
    recall        DOUBLE PRECISION,
    feature_list  JSONB,
    training_rows INTEGER,
    is_active     BOOLEAN NOT NULL DEFAULT FALSE
);

-- Only one active model at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_active
    ON model_registry (is_active) WHERE is_active;

-- ---------------------------------------------------------------------
-- Pipeline run audit log
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',   -- running/success/failed
    stage           TEXT,                              -- last stage reached
    batch_id        TEXT,
    model_id        TEXT,
    leads_processed INTEGER DEFAULT 0,
    leads_assigned  INTEGER DEFAULT 0,
    leads_pooled    INTEGER DEFAULT 0,
    errors          TEXT
);

-- ---------------------------------------------------------------------
-- Per-run intermediates (kept for explainability / debugging)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eligibility_matrix (
    run_id          TEXT NOT NULL,
    lead_id         TEXT NOT NULL,
    manager_id      TEXT NOT NULL,
    eligible        BOOLEAN NOT NULL,
    rejection_reason TEXT,
    PRIMARY KEY (run_id, lead_id, manager_id)
);

CREATE INDEX IF NOT EXISTS idx_elig_run_lead ON eligibility_matrix (run_id, lead_id);

CREATE TABLE IF NOT EXISTS scores (
    run_id                TEXT NOT NULL,
    lead_id               TEXT NOT NULL,
    manager_id            TEXT NOT NULL,
    conversion_probability DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (run_id, lead_id, manager_id)
);

CREATE INDEX IF NOT EXISTS idx_scores_run_lead ON scores (run_id, lead_id);

-- ---------------------------------------------------------------------
-- Outputs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assignments (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             TEXT NOT NULL,
    lead_id            TEXT NOT NULL,
    primary_manager_id TEXT NOT NULL,
    fallback_manager_id TEXT,
    confidence_score   DOUBLE PRECISION,
    match_score        DOUBLE PRECISION,
    intent_bucket      TEXT,
    assigned_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    push_status        TEXT NOT NULL DEFAULT 'pending',  -- pending/success/failed
    UNIQUE (run_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_assign_run     ON assignments (run_id);
CREATE INDEX IF NOT EXISTS idx_assign_manager ON assignments (primary_manager_id);
CREATE INDEX IF NOT EXISTS idx_assign_push    ON assignments (push_status);

CREATE TABLE IF NOT EXISTS pool (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL,
    lead_id       TEXT NOT NULL,
    intent_bucket TEXT NOT NULL,
    priority_rank INTEGER NOT NULL,
    best_score    DOUBLE PRECISION,
    reason        TEXT,                                -- why it landed in the pool
    status        TEXT NOT NULL DEFAULT 'available',   -- available/claimed
    claimed_by    TEXT,
    claimed_at    TIMESTAMPTZ,
    UNIQUE (run_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_pool_run_status ON pool (run_id, status);
CREATE INDEX IF NOT EXISTS idx_pool_rank       ON pool (run_id, priority_rank);
