# AI-Powered Intelligent Lead Assignment Engine

Hackathon project (team: DataDynamos) that assigns pre-classified sales leads
(H/M/L/EL intent) to the sales manager most likely to convert them, capped at 50
leads per manager, with overflow routed to a claimable pool.

See [PLAN.md](./PLAN.md) for the full design and task breakdown.

## Quick start

Requires **Python 3.11+** and **Docker** (with the Compose plugin).

```bash
# 1. Clone
git clone git@github.com:NishitSingh2023/conversion_catalyst.git
cd conversion_catalyst

# 2. Virtualenv + dependencies (runtime + dev/test/dashboard tooling)
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt

# 3. Start Postgres (host port 5433 to avoid clashing with a local 5432)
docker compose up -d postgres
export DB_PORT=5433

# 4. Schema + sample data
python scripts/apply_migrations.py
python data/generate_sample_data.py
python scripts/load_sample_data.py

# 5. Train an initial model (writes to models/ and activates it)
ENVIRONMENT=local python training/train.py

# 6. Run the test suite
pytest
```

Runtime-only install (no test/dashboard tooling): `pip install -r requirements.txt`.

## Pipeline

```
new_leads (H/M/L/EL)  ->  Eligibility  ->  Scoring (XGBoost)  ->  Optimizer (Hungarian/LP)
                                                                        |
                 lead_manager_history  ->  train model + derive manager profiles
                                                                        v
                                        assignments (+ fallback) / pool  ->  LSQ bulk API
```

All input and output data lives in **PostgreSQL**; a **Streamlit** dashboard reads
directly from it. Manager attributes are derived entirely from
`lead_manager_history` — there is no separate managers table.

## Repository layout

| Path | Purpose |
|------|---------|
| `PLAN.md` | Full implementation plan |
| `Dockerfile` | Single image all stages run from |
| `terraform/` | VPC, RDS, S3, ECR, Lambda, Step Functions, EventBridge, Secrets Manager |
| `shared/` | Config, DB access, feature engineering, manager profiles, model IO, run tracking |
| `lambdas/` | Stage handlers: `ingest`, `eligibility`, `scoring`, `optimizer`, `pool`, `lsq_push`, plus `migrate` |
| `training/` | XGBoost training job and its Lambda entrypoint |
| `db_migrations/` | SQL schema migrations |
| `data/` | Sample data generator |
| `dashboard/` | Streamlit dashboard |
| `tests/` | Unit and integration tests |
| `scripts/` | Local helpers and the image build/push script |

## Local development

Requires Docker and Python 3.11+.

```bash
# 1. Virtualenv and dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Start Postgres. Host port is 5433 to avoid clashing with an existing
#    local Postgres on 5432, so export DB_PORT for every command below.
docker compose up -d postgres
export DB_PORT=5433

# 3. Apply the schema
python scripts/apply_migrations.py

# 4. Generate and load sample data
python data/generate_sample_data.py
python scripts/load_sample_data.py

# 5. Train a model (writes to models/ and activates it in model_registry)
ENVIRONMENT=local python training/train.py

# 6. Run tests. Integration tests provision their own database and skip
#    automatically if Postgres is unreachable.
pytest
```

## Configuration

Runtime config resolves from AWS Secrets Manager when `DB_SECRET_ARN` /
`LSQ_SECRET_ARN` are set, otherwise from environment variables (see
`shared/config.py`). No secret values live in the repository.

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_HOST` / `DB_PORT` / `DB_NAME` | `localhost` / `5432` / `lead_assignment` | Postgres connection |
| `ENVIRONMENT` | `local` | `local` writes model artifacts to `models/`, otherwise S3 |
| `REJECTION_SAMPLE_LEADS` | `200` | Leads whose eligibility rejections are persisted |

## Deploying to AWS

Order matters: the functions reference an image tag that must already exist in
ECR, and the schema must be applied from inside the VPC.

```bash
# 1. Build and push the image (creates the ECR repo if needed)
./scripts/build_and_push.sh

# 2. Provision infrastructure
cd terraform
terraform init
terraform apply -var-file=example.tfvars -var="image_tag=latest"

# 3. Create the schema in RDS. It is in private subnets with no public access,
#    so this runs as an in-VPC Lambda. `terraform output migrate_command`
#    prints this exact line.
aws lambda invoke --function-name lead-assignment-dev-migrate /dev/stdout

# 4. Train an initial model so scoring has an active model to resolve
aws lambda invoke --function-name lead-assignment-dev-train /dev/stdout
```

### Notes on the infrastructure

- **Container images, not zips.** Dependencies total ~820MB (xgboost alone is
  417MB) against Lambda's 250MB zip ceiling. Even the optimizer, which loads no
  model, reaches 249MB from scipy/pandas/numpy. One image serves all stages and
  each function selects its entrypoint via a CMD override, so no stage can drift
  onto different library versions than the one training used.
- **RDS is private.** No public accessibility and ingress only from the Lambda
  security group. Migrations therefore go through the `migrate` function.
- **Egress is opt-in.** `enable_nat_gateway` defaults to `false`. The LSQ push
  needs a NAT gateway to reach a real API, and NAT bills hourly, so turn it on
  only when pointing `lsq_api_base_url` at a live sandbox. Left off, the push
  hangs until timeout.
- **Secrets.** The DB password is generated by Terraform and stored only in
  Secrets Manager. The LSQ API key is created as a placeholder and must be
  populated out of band.
