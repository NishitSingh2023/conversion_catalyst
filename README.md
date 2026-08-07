# AI-Powered Intelligent Lead Assignment Engine

Hackathon project (team: DataDynamos) that intelligently assigns pre-classified sales
leads (H/M/L/EL intent) to the sales manager most likely to convert them, while
respecting a per-manager workload cap and routing overflow to a claimable pool.

See [PLAN.md](./PLAN.md) for the full design, architecture, and task breakdown.

## Pipeline at a glance

```
new_leads (H/M/L/EL)  ->  Eligibility Filter  ->  Scoring (XGBoost)  ->  Optimizer (Hungarian/LP)
                                                                              |
                       lead_manager_history  ->  train model + derive manager profiles
                                                                              v
                                                        assignments (+ fallback) / pool  ->  LSQ bulk API
```

All input and output data lives in **PostgreSQL**. A **Streamlit** dashboard reads
directly from Postgres to visualise the end-to-end flow.

## Repository layout

| Path | Purpose |
|------|---------|
| `PLAN.md` | Full implementation plan |
| `terraform/` | Infrastructure as code (VPC, RDS, S3, Lambda, Step Functions, EventBridge, IAM, Secrets Manager) |
| `shared/` | Shared Python library (config, DB access, feature engineering, manager profiles) used by lambdas + training |
| `lambdas/` | Lambda handlers: `ingest`, `eligibility`, `scoring`, `optimizer`, `pool`, `lsq_push` |
| `training/` | Model training job (XGBoost/LightGBM) |
| `db_migrations/` | SQL schema migrations |
| `data/` | Sample data generator + team-provided sample CSVs |
| `dashboard/` | Streamlit dashboard |
| `tests/` | Unit + integration tests |
| `scripts/` | Local helper scripts (load data, run pipeline locally) |

## Local development

```bash
# 1. Create a virtualenv and install dev dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Start a local Postgres (docker) and apply migrations
docker compose up -d postgres
python scripts/apply_migrations.py

# 3. Generate + load sample data
python data/generate_sample_data.py
python scripts/load_sample_data.py

# 4. Run the full pipeline locally (no AWS required)
python scripts/run_pipeline_local.py

# 5. Launch the dashboard
streamlit run dashboard/app.py
```

## Configuration

All runtime configuration is read from environment variables (see `shared/config.py`).
Database credentials and the LSQ API key are expected to come from **AWS Secrets
Manager** in deployed environments; locally they fall back to environment variables.

## Deploying to AWS

```bash
cd terraform
terraform init
terraform validate
terraform plan  -var-file=example.tfvars
terraform apply -var-file=example.tfvars
```

> Security: RDS runs in private subnets and is never exposed publicly. The LSQ API key
> and DB password are stored in AWS Secrets Manager, never hardcoded.
