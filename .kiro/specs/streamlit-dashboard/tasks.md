# Implementation Plan: Streamlit Dashboard

## Overview

Build the read-only Streamlit dashboard in Python (the repo language) as the
`dashboard/` package described in the design's Project Structure. Order follows
the dependency direction of the architecture diagram: package scaffolding, then
the pure helpers in `format.py` (the property-test targets), then the
`data.py` SELECT-only data-access layer, then the `app.py` bootstrap and run
selector, then the seven view pages, then integration coverage against the
migrated test database, and finally full-suite verification.

Every step builds on the previous one and nothing is left unwired: helpers are
consumed by `data.py`/pages, every `data.py` function is called by a page, and
every page is registered in `app.py`'s `st.navigation`.

## Tasks

- [ ] 1. Scaffold the dashboard package and confirm dependencies
  - [ ] 1.1 Create the `dashboard/` package skeleton
    - Create `dashboard/__init__.py` and `dashboard/pages/__init__.py` (empty package markers)
    - Create empty module placeholders `dashboard/format.py`, `dashboard/data.py`, `dashboard/app.py` with module docstrings stating the read-only contract
    - Do not import `shared.db.write_dataframe` or `shared.db.execute` anywhere under `dashboard/`
    - _Design: Project Structure, Security & Safety_
    - _Requirements: 1.2, 1.3_

  - [ ] 1.2 Confirm and pin dashboard and property-test dependencies
    - Verify `requirements-dev.txt` already pins `streamlit==1.36.0` and `plotly==5.22.0` (the `st.Page` / `st.navigation` API requires >= 1.36.0); leave as-is if present
    - Add a pinned `hypothesis` entry to `requirements-dev.txt` (needed for the property tests in task 2); no other new dependency
    - _Design: Streamlit and multipage approach, Testing Strategy_
    - _Requirements: 11.3_

- [ ] 2. Implement the pure formatting and pagination helpers (`dashboard/format.py`)
  - [ ] 2.1 Implement the null, probability, percent, array, and confidence formatters
    - `na_or(value)`: return exactly `"N/A"` for `None`/`NaN`, otherwise a formatted string
    - `format_probability(value, precision)`: fixed-precision rendering of a 0..1 float, `"N/A"` for null
    - `format_percent(value, precision)`: fixed-precision percent rendering, `"N/A"` for null
    - `format_text_array(value)`: render a Postgres `text[]` (Python list) as a readable string; a defined placeholder for the empty list, never an exception
    - `format_confidence(confidence_score, match_score, fallback_manager_id)`: when `fallback_manager_id` is null, display the primary `match_score` and label it as such; otherwise display the stored `confidence_score`
    - No Streamlit and no DB imports in this module — pure functions only
    - _Design: format.py conventions, Error Handling (Empty and loading states)_
    - _Requirements: 5.2, 6.1, 7.6, 8.5_

  - [ ] 2.2 Implement the pagination helper
    - `page_window(page_index, page_size) -> (limit, offset)` with `offset = page_index * page_size`, `limit = page_size`, clamping a negative page index to 0 so `offset` is never negative
    - Add `page_count(total_rows, page_size)` used by the view prev/next controls
    - _Design: In-app data structures (Pagination parameters)_
    - _Requirements: 11.1, 11.2_

  - [ ]* 2.3 Write property test for null rendering
    - **Property 1: Null values always render as "N/A"** — for any null (`None`/`NaN`) input the formatter returns exactly `"N/A"`, and for any non-null numeric input it returns a non-`"N/A"` string
    - Minimum 100 iterations; tag the test `Feature: streamlit-dashboard, Property 1: Null values always render as "N/A"`
    - File: `tests/test_dashboard_format_properties.py`
    - **Validates: Requirements 5.2, 8.5**

  - [ ]* 2.4 Write property test for probability formatting
    - **Property 2: Probability formatting is precise and bounded** — for any float in 0..1 the parsed value of the formatted string is within the formatter's rounding tolerance of the input and never exceeds the displayed precision
    - Minimum 100 iterations; tag the test `Feature: streamlit-dashboard, Property 2: Probability formatting is precise and bounded`
    - File: `tests/test_dashboard_format_properties.py`
    - **Validates: Requirements 5.1, 9.3**

  - [ ]* 2.5 Write property test for array rendering
    - **Property 3: Array rendering preserves membership** — for any list of strings (including the empty list) the rendered string contains every element; the empty list maps to a defined placeholder rather than raising
    - Minimum 100 iterations; tag the test `Feature: streamlit-dashboard, Property 3: Array rendering preserves membership`
    - File: `tests/test_dashboard_format_properties.py`
    - **Validates: Requirements 6.1**

  - [ ]* 2.6 Write property test for confidence fallback display
    - **Property 4: Confidence falls back to match score when no fallback manager** — for any row with null `fallback_manager_id` the displayed confidence equals `match_score`; when a fallback exists it equals the stored `confidence_score`
    - Minimum 100 iterations; tag the test `Feature: streamlit-dashboard, Property 4: Confidence falls back to match score when no fallback manager`
    - File: `tests/test_dashboard_format_properties.py`
    - **Validates: Requirements 7.6**

  - [ ]* 2.7 Write property test for the pagination helper
    - **Property 5: Pagination parameters are always valid and bounded** — for any page index and page size the helper yields a non-negative `offset` and a `limit` equal to the page size, so every generated read carries a bound
    - Minimum 100 iterations; tag the test `Feature: streamlit-dashboard, Property 5: Pagination parameters are always valid and bounded`
    - File: `tests/test_dashboard_pagination_properties.py`
    - **Validates: Requirements 11.1, 11.2**

  - [ ]* 2.8 Write unit tests for the formatter edge cases
    - Example-based cases the properties do not pin: `pandas.NaT`/null timestamps, `Decimal` metric values, non-ASCII array elements, precision boundaries
    - File: `tests/test_dashboard_format_units.py`
    - _Requirements: 5.2, 6.1, 8.5_

- [ ] 3. Checkpoint - helpers verified
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Implement the read-only data-access layer (`dashboard/data.py`)
  - [ ] 4.1 Create the module skeleton, connection probe, and caching conventions
    - Every public function takes explicit params and calls `shared.db.read_sql(SQL, params)`; import only `read_sql` from `shared.db` so no write path is reachable
    - `probe_connection()`: run a trivial `SELECT 1` and return a success flag plus the non-secret `host`, `port`, `dbname` read explicitly off `shared.config.get_config()` (never dump the config object, never touch the password)
    - Bind `:cap` from `shared.constants.MAX_LEADS_PER_MANAGER` and expose `INTENT_PRIORITY` for ordering where needed
    - Establish the `@st.cache_data(ttl=...)` convention: 300s for slow-changing reads, 30s for the run list, uncached or 15s for run-scoped reads, with `run_id` always in the cache key
    - _Design: Data-Access Layer Design, Caching Strategy, Security & Safety_
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 2.5, 11.3_

  - [ ] 4.2 Implement the run list query
    - `get_runs()`: `SELECT run_id, started_at, completed_at, status, stage, leads_processed, leads_assigned, leads_pooled FROM pipeline_runs ORDER BY started_at DESC`, cached `ttl=30`
    - Serves both the sidebar selector and the Run History view; the first row is the default Selected_Run so no separate "latest run" query exists
    - Return an empty DataFrame (not an exception) when the table has no rows
    - _Design: Run selection queries, Run History queries_
    - _Requirements: 3.1, 3.2, 3.4, 10.1, 10.2, 10.4_

  - [ ] 4.3 Implement the Pipeline Flow queries
    - `get_run_header(run_id)`: run row including `errors`, scoped `WHERE run_id = :run_id`
    - `get_funnel_reconciliation(run_id)`: single query returning `assigned`, `pooled`, and `reconciled_total` via scalar subqueries over `assignments` and `pool` — aggregation in SQL, not pandas
    - `get_pool_reason_breakdown(run_id)`: `GROUP BY reason`
    - _Design: Pipeline Flow queries_
    - _Requirements: 1.4, 4.1, 4.2, 4.3, 4.4, 4.5, 11.4_

  - [ ] 4.4 Implement the active model query
    - `get_active_model()`: `SELECT model_id, trained_at, auc, precision, recall, training_rows, feature_list FROM model_registry WHERE is_active`, cached `ttl=300`
    - Return an empty DataFrame when no active model exists
    - _Design: Model query, Caching Strategy_
    - _Requirements: 5.1, 5.4, 5.5, 11.3_

  - [ ] 4.5 Implement the Manager Profiles and per-agent capacity queries
    - `get_manager_profiles()`: explicit column list with `conv_rate_h AS "conv_rate_H"`, `conv_rate_m AS "conv_rate_M"`, `conv_rate_l AS "conv_rate_L"`, ordered by `manager_id`, cached `ttl=300`; `manager_name` is not in the SELECT list
    - `get_agent_capacity(run_id, cap)`: `manager_profiles` LEFT JOIN a `GROUP BY primary_manager_id` subquery over `assignments` for the run, returning `current_run_assignments` and `GREATEST(0, :cap - COALESCE(...))` as `remaining_capacity`, aggregated in SQL
    - `manager_profiles.manager_id` is the primary key, so the table grain already satisfies the per-agent grouping
    - _Design: Manager Profiles queries, Cross-cutting SQL notes_
    - _Requirements: 1.4, 2.6, 2.7, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 11.4_

  - [ ] 4.6 Implement the Assignments queries
    - `get_assignments(run_id, limit, offset)`: paged rows ordered by `lead_id`
    - `count_assignments(run_id)`: `count(*)` driver for page navigation
    - `get_push_status_breakdown(run_id)`: `GROUP BY push_status`
    - `get_agent_assignment_distribution(run_id, cap)`: `GROUP BY primary_manager_id` returning `assignment_count` and `(count(*) = :cap) AS at_cap`, ordered by count desc then `manager_id`
    - All scoped `WHERE run_id = :run_id`; no `manager_name` selected
    - _Design: Assignments queries_
    - _Requirements: 1.4, 7.1, 7.2, 7.3, 7.4, 7.5, 11.1, 11.4_

  - [ ] 4.7 Implement the Pool queries
    - `get_pool(run_id, limit, offset)`: paged rows ordered by `priority_rank ASC`
    - `count_pool(run_id)`: `count(*)` driver
    - `get_pool_status_breakdown(run_id)`: `GROUP BY status`; reuse `get_pool_reason_breakdown` from 4.3 for the reason breakdown
    - _Design: Pool queries_
    - _Requirements: 1.4, 8.1, 8.2, 8.3, 8.4, 11.1, 11.4_

  - [ ] 4.8 Implement the Explainability queries
    - `get_lead_attributes(lead_id)`: `lead_id, intent_bucket, geography, language, product_interest` from `new_leads`
    - `get_eligible_agents(run_id, lead_id, limit, offset)`: `eligibility_matrix` where `eligible`, ordered by `manager_id`
    - `get_lead_scores(run_id, lead_id, limit, offset)`: `scores` ordered by `conversion_probability DESC`
    - `get_sampled_rejections(run_id, lead_id, limit, offset)`: `eligibility_matrix` where `NOT eligible`, with `rejection_reason`
    - `has_rejection_rows(run_id, lead_id)`: `SELECT EXISTS (...) AS has_rejections` presence check driving the "not sampled" message
    - `get_default_interesting_leads(run_id)`: highest-`confidence_score` assignment `lead_id` (`NULLS LAST`, `LIMIT 1`) and the lowest-`priority_rank` `capacity_overflow` pool `lead_id` (`LIMIT 1`)
    - Every per-pair read is scoped by both `run_id` and `lead_id` and carries `:limit`/`:offset`; the cross-join is never selected unbounded
    - _Design: Explainability queries_
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 11.1, 11.2_

- [ ] 5. Checkpoint - data layer verified
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement the app entry point (`dashboard/app.py`)
  - [ ] 6.1 Wire the connection bootstrap and friendly failure message
    - Set page config, then call `data.probe_connection()`; on failure render "Cannot connect to database `<dbname>` at `<host>:<port>`" and `st.stop()` — no stack trace, no credentials, nothing logged from the config object
    - _Design: app.py responsibilities, Security & Safety_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [ ] 6.2 Implement the sidebar run selector and shared session state
    - Load `data.get_runs()`; if empty, render the "No runs are available." Empty_State and skip page dispatch without raising
    - Default `st.session_state["selected_run"]` to `run_list[0].run_id` (most recent `started_at`) when unset
    - Render the sidebar `st.selectbox` labeled `run_id | started_at | status` and write the choice back to `st.session_state["selected_run"]` so every page reads the same run on the same rerun
    - _Design: Run Selection & State_
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [ ] 6.3 Register the seven pages and the global cache-refresh control
    - Build `st.Page` entries for Pipeline Flow, Model, Manager Profiles, Assignments, Pool, Explainability, Run History in that order with explicit titles, and dispatch via `st.navigation(...).run()`
    - Add a sidebar "Refresh cached data" button that calls `st.cache_data.clear()`, serving as the manual refresh control for the cached Model and Manager Profiles reads
    - _Design: Streamlit and multipage approach, Caching Strategy_
    - _Requirements: 5.5, 6.6, 11.3_

- [ ] 7. Implement the seven view pages
  - [ ] 7.1 Implement the Pipeline Flow view (`dashboard/pages/pipeline_flow.py`)
    - Read `selected_run` from session state; call the run-header, funnel-reconciliation, and pool-by-reason queries inside `st.spinner`
    - Metric row for `status`, `stage`, `started_at`, `completed_at`; second row for `leads_processed`, `leads_assigned`, `leads_pooled`
    - Reconciliation callout comparing `reconciled_total` (assigned + pooled) against `leads_processed`, plus the assigned/pooled split
    - Pool reason breakdown for `no_eligible_manager` and `capacity_overflow`, rendering a missing reason as `0`
    - When `status = 'failed'`, show `errors` in an `st.error` block; null `completed_at` renders `"N/A"` via `format.py`
    - _Design: Per-View Design 1_
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [ ] 7.2 Implement the Model view (`dashboard/pages/model.py`)
    - Render `model_id` and `trained_at` in the header; `st.metric`s for `auc`, `precision`, `recall`, `training_rows` with null metrics shown as `"N/A"`
    - Render `feature_list` (JSONB) as a list inside an expander
    - Fixed caveat line: model metrics are high-variance when the positive class is small
    - Empty result -> "No active model is available." Empty_State, no exception
    - _Design: Per-View Design 2_
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ] 7.3 Implement the Manager Profiles view (`dashboard/pages/manager_profiles.py`)
    - Join the cached profiles frame to the run-scoped capacity frame on `manager_id`; render as a searchable/paginated `st.dataframe`
    - Render `languages_handled`, `geographies_handled`, `products_handled` through `format.format_text_array`; show `conv_rate_H`/`conv_rate_M`/`conv_rate_L` under those displayed names
    - Show `current_run_assignments` and `remaining_capacity` against the cap of 50
    - Null `avg_response_mins` / `last_active_date` -> `"N/A"`; no `manager_name` column anywhere
    - Empty result -> "No manager profiles are available." Empty_State
    - _Design: Per-View Design 3_
    - _Requirements: 2.6, 2.7, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [ ] 7.4 Implement the Assignments view (`dashboard/pages/assignments.py`)
    - Push-status breakdown metrics/bar on top; agent-wise distribution chart with `at_cap` agents (count = 50) visually highlighted
    - Paged `st.dataframe` of assignment rows driven by `format.page_window` and the `count(*)` query, with prev/next controls
    - Primary and fallback agents shown by `manager_id`; null `fallback_manager_id` -> `"N/A"` with confidence rendered via `format.format_confidence` so a no-fallback row displays the primary match score
    - Empty result -> "No assignments for this run."
    - _Design: Per-View Design 4_
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 11.1_

  - [ ] 7.5 Implement the Pool view (`dashboard/pages/pool.py`)
    - Reason and status breakdown metrics/bars from the SQL aggregates
    - Paged `st.dataframe` ordered by `priority_rank ASC` with prev/next controls
    - Null `best_score` -> `"N/A"`; null `claimed_by` / `claimed_at` on available entries -> `"N/A"`
    - Empty result -> "No pooled leads for this run."
    - _Design: Per-View Design 5_
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 11.1_

  - [ ] 7.6 Implement the Explainability view (`dashboard/pages/explainability.py`)
    - `lead_id` picker plus two quick-pick buttons seeded from `get_default_interesting_leads` (highest-confidence assignment, a `capacity_overflow` pool lead); preselect the highest-confidence lead on first open
    - Render lead attributes, then the scored-agents table ordered by probability desc, then the eligible-agents list, both paged
    - Gate the rejections section on `has_rejection_rows`: when false render "Rejection detail was not sampled for this lead", never "no managers were rejected"
    - Persistent caveat that rejection reasons are captured for a sampled subset of leads
    - Unknown `lead_id` -> "Lead not found in this run's batch."
    - _Design: Per-View Design 6_
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 11.1_

  - [ ] 7.7 Implement the Run History view (`dashboard/pages/run_history.py`)
    - `st.dataframe` of `pipeline_runs` ordered by `started_at DESC` with the columns from the run list query
    - A select control that writes `st.session_state["selected_run"]` and reruns, so selecting here behaves identically to the sidebar
    - Null `completed_at` -> `"N/A"`; empty result -> "No run history is available."
    - _Design: Per-View Design 7, Run Selection & State_
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 8. Checkpoint - all views render
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Add integration coverage for the data-access layer
  - [ ] 9.1 Write a run-seeding helper for integration tests
    - `tests/dashboard_seed.py`: insert a minimal deterministic fixture into the migrated test DB — two `pipeline_runs` rows, a handful of `manager_profiles` (including one with a non-null `manager_name` so anonymization is actually exercised), `new_leads`, `scores`, `eligibility_matrix` (eligible and sampled-rejection rows), `assignments` (one with null `fallback_manager_id`, one manager at the 50 cap), `pool` rows for both reasons, and an active `model_registry` row
    - Build on the existing session-scoped `db` fixture in `tests/conftest.py` so the helper reuses the provisioned `lead_assignment_test` database
    - _Design: Testing Strategy_
    - _Requirements: 1.1, 3.2_

  - [ ]* 9.2 Write integration tests for query correctness and run scoping
    - Use the `db` fixture so the module skips cleanly when Postgres is unreachable
    - Assert every `data.py` read parses and executes; assert run-scoped reads return only rows for the requested `run_id` (seed two runs and check the second run's rows never appear)
    - Assert `get_manager_profiles()` returns the mixed-case columns `conv_rate_H`, `conv_rate_M`, `conv_rate_L`
    - Assert the SQL-side aggregates match: funnel `reconciled_total = assigned + pooled`, push-status and pool reason/status breakdown totals, `at_cap` true exactly for the manager seeded at 50, and `remaining_capacity` never negative
    - Assert paged reads respect `:limit`/`:offset` and that `has_rejection_rows` is false for a lead seeded without rejection rows
    - File: `tests/test_dashboard_data.py`
    - _Requirements: 1.1, 1.4, 3.3, 6.3, 6.5, 7.4, 9.5, 11.1, 11.4_

  - [ ]* 9.3 Write anonymization and read-only guard tests
    - Assert no DataFrame returned by any `data.py` function contains a `manager_name` column
    - Statically assert the `dashboard/` source contains no `INSERT`, `UPDATE`, `DELETE`, or DDL keyword and no `manager_name` literal, and that no module under `dashboard/` imports `write_dataframe`, `execute`, or `session_scope`
    - File: `tests/test_dashboard_data_anonymization.py`
    - _Requirements: 1.1, 1.2, 1.3, 2.6, 2.7_

- [ ] 10. Final wiring and verification
  - [ ]* 10.1 Write an import-smoke test for the app and page modules
    - Import `dashboard.app`, `dashboard.data`, `dashboard.format`, and all seven page modules to catch wiring, syntax, and unused-import regressions; assert all seven pages are registered in the navigation list
    - File: `tests/test_dashboard_smoke.py`
    - _Requirements: 3.3_

  - [ ] 10.2 Run the full verification pass and fix fallout
    - Run `ruff check .` and the full suite with `DB_PORT=5433 pytest` (Postgres up via `docker compose up -d postgres`) so both the pure-function and DB-touching tiers execute; fix any failures
    - Confirm every `data.py` function is referenced by at least one page and every page is registered in `app.py` — no orphaned code
    - Manual step for the user (do not run from the agent, it is a long-running server): `DB_PORT=5433 streamlit run dashboard/app.py`, then confirm all seven views render against the committed run data
    - _Requirements: 1.1, 1.2, 2.2, 3.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Implementation language is Python, matching the existing repo (`shared/`, `lambdas/`, `training/`).
- Property tests come from the design's Correctness Properties section: minimum 100 iterations each, tagged `Feature: streamlit-dashboard, Property {n}: {text}`.
- Integration tests reuse the session-scoped `db` fixture in `tests/conftest.py`, which provisions `lead_assignment_test`, applies `db_migrations/*.sql`, and skips when Postgres is unreachable.
- Checkpoints at tasks 3, 5, and 8 keep validation incremental.
- The read-only guarantee is structural: `data.py` is the only module touching `shared.db`, and it imports `read_sql` only.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "4.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.2", "9.1"] },
    { "id": 3, "tasks": ["2.4", "4.3"] },
    { "id": 4, "tasks": ["2.5", "4.4"] },
    { "id": 5, "tasks": ["2.6", "4.5"] },
    { "id": 6, "tasks": ["2.7", "2.8", "4.6"] },
    { "id": 7, "tasks": ["4.7"] },
    { "id": 8, "tasks": ["4.8"] },
    { "id": 9, "tasks": ["6.1"] },
    { "id": 10, "tasks": ["6.2", "9.2"] },
    { "id": 11, "tasks": ["6.3", "9.3"] },
    { "id": 12, "tasks": ["7.1", "7.2", "7.3", "7.4", "7.5", "7.6", "7.7"] },
    { "id": 13, "tasks": ["10.1"] },
    { "id": 14, "tasks": ["10.2"] }
  ]
}
```
