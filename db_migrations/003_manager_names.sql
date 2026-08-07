-- =====================================================================
-- Real-dataset manager names
-- =====================================================================
-- The synthetic generator only ever produced opaque manager ids (MGR0001).
-- The real team dataset (lead_rep_dataset.csv) carries a human REP_NAME for
-- each REP_ID, which is far more useful in the dashboard and LSQ push than a
-- bare uuid. REP_ID stays the join key (it is stable and unique; names have
-- nulls and at least one collision), and the name rides along for display.
--
--   * lead_manager_history.manager_name - the name as it appeared on the row,
--     denormalised so the single-source-of-truth table still owns everything.
--   * manager_profiles.manager_name      - the modal name per manager, derived
--     alongside the rest of the profile.
-- Both are nullable; anything missing falls back to the manager_id downstream.
-- =====================================================================

ALTER TABLE lead_manager_history
    ADD COLUMN IF NOT EXISTS manager_name TEXT;

ALTER TABLE manager_profiles
    ADD COLUMN IF NOT EXISTS manager_name TEXT;
