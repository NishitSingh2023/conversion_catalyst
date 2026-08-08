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

Everything deploys to **us-west-2 (Oregon)**, which is the default for
`aws_region`. Deploying elsewhere is a one-variable change, but note that a
call to the wrong region typically surfaces as an "access denied" error rather
than anything region-shaped, so check the region first when a command is
refused.

Order matters in two places. A container-image Lambda will not create unless the
image tag already exists in ECR, and the ECR repository is itself a
Terraform-managed resource, so the repository has to be created before the image
is pushed and the image pushed before the functions are created. The schema then
has to be applied from inside the VPC, because RDS is private.

```bash
export AWS_REGION=us-west-2

# 1. Create just the ECR repository, so the push has somewhere to go and
#    Terraform still owns the resource. Building the repo any other way leaves
#    it outside state and the full apply in step 3 fails with
#    RepositoryAlreadyExistsException.
cd terraform
terraform init
terraform apply -var-file=example.tfvars -target=aws_ecr_repository.pipeline

# 2. Build and push the image
cd .. && ./scripts/build_and_push.sh

# 3. Provision everything else
cd terraform
terraform apply -var-file=example.tfvars -var="image_tag=latest"

# 4. Create the schema in RDS. It is in private subnets with no public access,
#    so this runs as an in-VPC Lambda. `terraform output migrate_command`
#    prints this line with the region already filled in.
aws lambda invoke --function-name lead-assignment-dev-migrate \
  --region us-west-2 /dev/stdout

# 5. Train an initial model so scoring has an active model to resolve
aws lambda invoke --function-name lead-assignment-dev-train \
  --region us-west-2 /dev/stdout
```

Resource names are derived from `project_name` and `environment`, and only the
S3 bucket carries a random suffix. In a shared account, give each person their
own `environment` value so the IAM roles, ECR repository, RDS identifier,
secrets and log groups do not collide.

### Notes on the infrastructure

- **Container images, not zips.** Dependencies total ~820MB (xgboost alone is
  417MB) against Lambda's 250MB zip ceiling. Even the optimizer, which loads no
  model, reaches 249MB from scipy/pandas/numpy. One image serves all stages and
  each function selects its entrypoint via a CMD override, so no stage can drift
  onto different library versions than the one training used.
- **RDS is private.** No public accessibility and ingress only from the Lambda
  security group. Migrations therefore go through the `migrate` function.
- **Egress is opt-in.** `enable_nat_gateway` defaults to `false`, so there is no
  route to the internet and no hourly NAT charge. That is safe with the default
  `lsq_api_base_url`, because `lsq_push` recognises the `.local` sentinel and
  simulates the push, marking records pushed and logging the payload. Pointing
  `lsq_api_base_url` at a live sandbox without also setting
  `enable_nat_gateway = true` is the broken combination: the call has nowhere to
  go and each batch stalls for the 30s HTTP timeout.
- **What bills while idle.** The always-on costs are the RDS instance
  (`db.t3.micro`, 20GB gp3) and the Secrets Manager interface VPC endpoint, which
  is charged per AZ per hour and is created in both private subnets. Lambda,
  Step Functions and S3 are effectively free at this volume. Enabling
  `enable_nat_gateway` adds a NAT gateway and an Elastic IP on top.
- **Secrets.** The DB password is generated by Terraform and stored only in
  Secrets Manager. The LSQ API key is created as a placeholder and must be
  populated out of band.
