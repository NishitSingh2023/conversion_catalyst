-- =====================================================================
-- Robustness changes
-- =====================================================================
-- 1. new_leads.is_valid          - ingest validated leads but nothing excluded
--                                  them, so a lead with a bad intent bucket
--                                  reached scoring with every intent one-hot at
--                                  zero. Validity is now recorded and every
--                                  downstream stage filters on it.
-- 2. assignments.business_date   - the daily capacity window used current_date
--                                  in RDS (UTC) while the schedule fires at
--                                  04:00 IST. A retry crossing midnight UTC saw
--                                  zero load and could grant another 50 leads.
--                                  Capacity is now keyed on an explicit
--                                  business date owned by the pipeline.
-- 3. manager_daily_load          - one definition of "leads held today" shared
--                                  by eligibility, the optimizer and the pool
--                                  claim path, so the cap cannot be computed
--                                  three subtly different ways.
-- =====================================================================

ALTER TABLE new_leads
    ADD COLUMN IF NOT EXISTS is_valid         BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS validation_error TEXT;

CREATE INDEX IF NOT EXISTS idx_new_leads_valid ON new_leads (batch_id, is_valid);

ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS business_date DATE;

-- Backfill existing rows so the new window is consistent with history.
UPDATE assignments SET business_date = assigned_at::date WHERE business_date IS NULL;

CREATE INDEX IF NOT EXISTS idx_assign_business_date
    ON assignments (business_date, primary_manager_id);

-- Pool claims consume capacity too, so they must be visible to the same view.
CREATE INDEX IF NOT EXISTS idx_pool_claimed_by ON pool (claimed_by, status);

-- Single source of truth for how loaded a manager is on a given business date.
CREATE OR REPLACE VIEW manager_daily_load AS
SELECT
    business_date,
    manager_id,
    sum(load) AS load
FROM (
    -- Leads auto-assigned by the optimizer.
    SELECT business_date, primary_manager_id AS manager_id, count(*)::bigint AS load
    FROM assignments
    WHERE business_date IS NOT NULL
    GROUP BY 1, 2

    UNION ALL

    -- Leads a manager pulled from the pool themselves.
    SELECT claimed_at::date AS business_date, claimed_by AS manager_id, count(*)::bigint AS load
    FROM pool
    WHERE status = 'claimed' AND claimed_by IS NOT NULL
    GROUP BY 1, 2
) combined
GROUP BY business_date, manager_id;
