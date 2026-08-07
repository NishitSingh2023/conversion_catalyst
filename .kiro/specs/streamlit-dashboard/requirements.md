# Requirements Document

## Introduction

The Streamlit Dashboard is a read-only visualization surface for the AI-Powered
Intelligent Lead Assignment Engine (Task 11 in PLAN.md). Its audience is
hackathon judges who need to inspect what the nightly assignment pipeline did on
a given run without triggering any pipeline stage. The Dashboard connects
directly to the same PostgreSQL database every pipeline stage (profile build,
ingest, eligibility, scoring, optimizer, pool, LSQ push) writes to, and reads
only. It exposes seven views — Pipeline Flow, Model, Manager Profiles,
Assignments, Pool, Explainability, and Run History — all scoped by a selected
pipeline run that defaults to the most recent run.

The Dashboard surfaces decisions and outcomes that already exist in the
database. It does not compute assignments, re-run scoring, or write to any table.
It reuses the existing shared configuration and database helpers so that
credentials resolve consistently with the rest of the system and are never
exposed.

## Glossary

- **Dashboard**: The Streamlit read-only application specified by this document.
- **PostgreSQL_Database**: The shared relational database that stores all
  pipeline inputs, intermediates, and outputs. Same instance the pipeline writes
  to; locally a Docker Postgres reachable on `DB_PORT=5433`.
- **Run**: A single execution of the pipeline, identified by `run_id` in the
  `pipeline_runs` table. Scores, eligibility, assignments, and pool rows are all
  keyed by `run_id`.
- **Selected_Run**: The `run_id` currently chosen in the Dashboard run selector.
  All run-scoped views read data for this `run_id`.
- **Config_Resolver**: The existing `shared/config.py` resolution chain, which
  resolves configuration in the precedence order AWS Secrets Manager, then
  environment variables, then local defaults.
- **DB_Engine_Helper**: The existing cached SQLAlchemy engine accessor in
  `shared/db.py` (`get_engine`, `read_sql`).
- **Active_Model**: The single row in `model_registry` where `is_active` is true.
- **Manager_Profile**: A row in `manager_profiles`, derived from
  `lead_manager_history`. There is no separate managers table. In the Dashboard,
  each Manager_Profile is displayed and grouped by `manager_id` (the Agent
  identifier) only.
- **Agent**: A sales manager or representative, identified in the Dashboard
  solely by `manager_id`. The Dashboard shows no personal agent name. The
  `manager_profiles.manager_name` column still exists in the
  PostgreSQL_Database but is never displayed.
- **Assignment**: A row in `assignments` mapping one lead to a primary manager,
  an optional fallback manager, a confidence score, and a push status, for a run.
- **Pool_Entry**: A row in `pool` representing a valid, unassigned lead that is
  claimable, with a reason of `no_eligible_manager` or `capacity_overflow`.
- **Eligibility_Pair**: A row in `eligibility_matrix` recording whether a given
  manager was eligible for a given lead on a run, with an optional
  rejection reason.
- **Per_Manager_Cap**: The `MAX_LEADS_PER_MANAGER` constant, value 50, the
  maximum leads auto-assigned to one manager per run and business date.
- **Intent_Priority_Order**: The ordering H, then M, then L, then EL, from the
  `INTENT_PRIORITY` constant, used to prioritize assignment and pool ranking.
- **Confidence_Score**: `assignments.confidence_score`, defined as primary
  match score minus fallback match score, clipped to the range 0 to 1; equal to
  the primary match score when a lead has no fallback manager.
- **Rejection_Sampling**: The pipeline behavior where all eligible pairs are
  persisted to `eligibility_matrix` but rejection rows are sampled for a limited
  number of leads (env `REJECTION_SAMPLE_LEADS`, default 200), so rejection
  explainability is partial.
- **Secret_Value**: Any credential or sensitive identifier, including the
  database password, the LSQ API key, and secret ARNs.
- **Empty_State**: A rendered view result when a query returns zero rows.

## Requirements

### Requirement 1: Read-only data access

**User Story:** As a platform owner, I want the Dashboard to only read from the
database, so that displaying data to judges can never alter pipeline state.

#### Acceptance Criteria

1. THE Dashboard SHALL issue only SELECT queries against the PostgreSQL_Database.
2. THE Dashboard SHALL NOT issue INSERT, UPDATE, DELETE, or DDL statements against the PostgreSQL_Database.
3. THE Dashboard SHALL obtain database connections through the DB_Engine_Helper rather than constructing its own engine.
4. WHERE a view presents aggregate metrics, THE Dashboard SHALL compute the aggregation in a SQL query rather than in Python.

### Requirement 2: Configuration and connection resolution

**User Story:** As an operator, I want the Dashboard to resolve its database
connection through the existing configuration chain, so that it behaves
consistently across local and deployed environments.

#### Acceptance Criteria

1. THE Dashboard SHALL resolve database configuration through the Config_Resolver precedence order of AWS Secrets Manager, then environment variables, then local defaults.
2. WHERE the environment sets `DB_PORT` to 5433, THE Dashboard SHALL connect to the PostgreSQL_Database on port 5433.
3. IF a database connection attempt fails, THEN THE Dashboard SHALL display a message that identifies the non-secret host, port, and database name and SHALL NOT display a stack trace.
4. THE Dashboard SHALL NOT display any Secret_Value in the user interface.
5. THE Dashboard SHALL NOT write any Secret_Value to logs.
6. THE Dashboard SHALL NOT display `manager_profiles.manager_name` or any personal agent name in any view.
7. THE Dashboard SHALL identify each Agent solely by `manager_id` in every view.

### Requirement 3: Run selection and scoping

**User Story:** As a judge, I want to pick which pipeline run I am inspecting,
so that every view shows a consistent picture of that run.

#### Acceptance Criteria

1. WHEN the Dashboard loads, THE Dashboard SHALL set the Selected_Run to the pipeline run with the most recent `started_at`.
2. THE Dashboard SHALL present a run selector listing available runs from `pipeline_runs` identified by `run_id`, `started_at`, and `status`.
3. WHEN a judge selects a different run, THE Dashboard SHALL scope every run-scoped view to the newly Selected_Run.
4. IF `pipeline_runs` contains zero rows, THEN THE Dashboard SHALL render an Empty_State that states no runs are available and SHALL NOT raise an error.

### Requirement 4: Pipeline Flow view

**User Story:** As a judge, I want to see how the selected run moved leads
through the pipeline, so that I can understand the end-to-end outcome at a glance.

#### Acceptance Criteria

1. WHEN the Pipeline Flow view renders for the Selected_Run, THE Dashboard SHALL display the run `status`, `stage`, `started_at`, and `completed_at`.
2. THE Dashboard SHALL display the Selected_Run counts of `leads_processed`, `leads_assigned`, and `leads_pooled`.
3. THE Dashboard SHALL display the reconciliation of valid leads as assigned leads plus pooled leads for the Selected_Run.
4. THE Dashboard SHALL display the pool breakdown by reason `no_eligible_manager` and `capacity_overflow` for the Selected_Run.
5. IF the Selected_Run `status` is `failed`, THEN THE Dashboard SHALL display the run `errors` value.

### Requirement 5: Model view

**User Story:** As a judge, I want to see which model produced the scores, so
that I can assess the model behind the assignment decisions.

#### Acceptance Criteria

1. THE Dashboard SHALL display the Active_Model attributes `model_id`, `trained_at`, `auc`, `precision`, `recall`, `training_rows`, and `feature_list`.
2. IF a model metric value is null, THEN THE Dashboard SHALL display "N/A" for that metric.
3. THE Dashboard SHALL display a caveat that model metrics are high-variance when the positive class is small.
4. IF `model_registry` contains no active model, THEN THE Dashboard SHALL render an Empty_State that states no active model is available and SHALL NOT raise an error.
5. THE Dashboard SHALL cache the Active_Model read and SHALL provide a manual control to refresh the cached read.

### Requirement 6: Manager Profiles view

**User Story:** As a judge, I want to browse manager profiles, so that I can see
the derived capabilities and performance behind assignment eligibility.

#### Acceptance Criteria

1. THE Dashboard SHALL display Manager_Profile rows including `manager_id`, `languages_handled`, `geographies_handled`, `products_handled`, `conv_rate_overall`, `conv_rate_H`, `conv_rate_M`, `conv_rate_L`, `avg_response_mins`, `total_leads_handled`, `last_active_date`, and `derived_active_flag`.
2. THE Dashboard SHALL identify each Agent by `manager_id` and SHALL NOT include or display `manager_name`.
3. WHERE the PostgreSQL_Database returns the intent conversion-rate columns lower-cased, THE Dashboard SHALL map the lower-cased column names to the displayed high, medium, and low conversion-rate fields.
4. THE Dashboard SHALL present Manager_Profile data aggregated per Agent, grouped by `manager_id`.
5. WHEN the Manager Profiles view renders for the Selected_Run, THE Dashboard SHALL display each Agent's current-run assignment count and remaining capacity against the Per_Manager_Cap of 50.
6. THE Dashboard SHALL cache the Manager_Profile read and SHALL provide a manual control to refresh the cached read.
7. IF `manager_profiles` contains zero rows, THEN THE Dashboard SHALL render an Empty_State that states no manager profiles are available and SHALL NOT raise an error.

### Requirement 7: Assignments view

**User Story:** As a judge, I want to inspect the assignments for the selected
run, so that I can see which manager each lead was routed to and how confident
the engine was.

#### Acceptance Criteria

1. WHEN the Assignments view renders for the Selected_Run, THE Dashboard SHALL display Assignment rows including `lead_id`, `primary_manager_id`, `fallback_manager_id`, `confidence_score`, `match_score`, `intent_bucket`, `assigned_at`, and `push_status`.
2. THE Dashboard SHALL display the primary Agent and fallback Agent by `manager_id`.
3. THE Dashboard SHALL display the count of assignments grouped by `push_status` for the Selected_Run.
4. THE Dashboard SHALL display an agent-wise distribution of the assignment count per `manager_id` for the Selected_Run and SHALL highlight each Agent whose assignment count equals the Per_Manager_Cap of 50.
5. THE Dashboard SHALL limit the number of Assignment rows loaded per page and SHALL provide navigation across pages.
6. IF a lead has no fallback manager, THEN THE Dashboard SHALL display the Confidence_Score as equal to the primary match score.

### Requirement 8: Pool view

**User Story:** As a judge, I want to see the claimable pool for the selected
run, so that I can understand which leads were not auto-assigned and why.

#### Acceptance Criteria

1. WHEN the Pool view renders for the Selected_Run, THE Dashboard SHALL display Pool_Entry rows including `lead_id`, `intent_bucket`, `priority_rank`, `best_score`, `reason`, `status`, `claimed_by`, and `claimed_at`.
2. THE Dashboard SHALL order Pool_Entry rows by `priority_rank` ascending.
3. THE Dashboard SHALL display the count of Pool_Entry rows grouped by `reason` for the Selected_Run.
4. THE Dashboard SHALL display the count of Pool_Entry rows grouped by `status` for the Selected_Run.
5. IF `best_score` is null for a Pool_Entry, THEN THE Dashboard SHALL display "N/A" for that value.

### Requirement 9: Explainability view

**User Story:** As a judge, I want to trace why a specific lead was assigned or
pooled, so that I can validate the engine's decision for individual leads.

#### Acceptance Criteria

1. WHEN a judge selects a lead in the Explainability view, THE Dashboard SHALL display that lead's attributes from `new_leads` including `intent_bucket`, `geography`, `language`, and `product_interest`.
2. WHEN a judge selects a lead, THE Dashboard SHALL display the eligible Agents for that lead and Selected_Run from `eligibility_matrix`, identified by `manager_id`.
3. WHEN a judge selects a lead, THE Dashboard SHALL display the per-Agent `conversion_probability` values for that lead and Selected_Run from `scores`, identified by `manager_id`.
4. THE Dashboard SHALL display a caveat that rejection reasons are captured for a sampled subset of leads because of Rejection_Sampling.
5. IF the selected lead has no rejection rows in `eligibility_matrix` for the Selected_Run, THEN THE Dashboard SHALL display a message that rejection detail was not sampled for that lead rather than implying no managers were rejected.
6. THE Dashboard SHALL limit the number of per-pair rows loaded from `scores` and `eligibility_matrix` per page and SHALL provide navigation across pages.

### Requirement 10: Run History view

**User Story:** As a judge, I want to see the history of pipeline runs, so that
I can compare outcomes across runs.

#### Acceptance Criteria

1. THE Dashboard SHALL display `pipeline_runs` rows including `run_id`, `started_at`, `completed_at`, `status`, `stage`, `leads_processed`, `leads_assigned`, and `leads_pooled`.
2. THE Dashboard SHALL order Run History rows by `started_at` descending.
3. WHEN a judge selects a run in the Run History view, THE Dashboard SHALL set the Selected_Run to that run.
4. IF `pipeline_runs` contains zero rows, THEN THE Dashboard SHALL render an Empty_State that states no run history is available and SHALL NOT raise an error.

### Requirement 11: Performance and resource use

**User Story:** As a judge, I want views to load responsively even on large
tables, so that the demo stays smooth.

#### Acceptance Criteria

1. THE Dashboard SHALL page or limit reads from the per-pair tables `scores` and `eligibility_matrix`.
2. THE Dashboard SHALL NOT load a full cross-join of leads and managers into memory.
3. THE Dashboard SHALL cache reads of slow-changing data including the Active_Model and Manager_Profile data.
4. WHERE a view presents a count or aggregate over a per-pair table, THE Dashboard SHALL compute the aggregate in a SQL query.
