"""Runtime configuration.

Configuration is resolved in this order of precedence:

1. AWS Secrets Manager (when ``DB_SECRET_ARN`` / ``LSQ_SECRET_ARN`` are set) -
   used in deployed Lambda / Fargate environments.
2. Environment variables - used locally and as overrides.
3. Sensible local-development defaults.

No secret values are ever hardcoded in the repository.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.dbname}"
        )


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig
    model_bucket: str
    lsq_api_base_url: str
    lsq_api_key: str
    aws_region: str
    environment: str


def _load_secret_json(secret_arn: str) -> dict:
    """Fetch and parse a JSON secret from AWS Secrets Manager."""
    import boto3  # imported lazily so local dev without boto3 creds still works

    # AWS_REGION still wins: Lambda injects it at runtime. The fallback matters
    # only outside Lambda (local scripts, containers), and it is us-west-2
    # because the hackathon lab account is Oregon-only - a call to the wrong
    # region fails as "access denied" rather than anything region-shaped.
    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-west-2"))
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _resolve_database_config() -> DatabaseConfig:
    secret_arn = os.getenv("DB_SECRET_ARN")
    if secret_arn:
        secret = _load_secret_json(secret_arn)
        return DatabaseConfig(
            host=secret.get("host", os.getenv("DB_HOST", "localhost")),
            port=int(secret.get("port", os.getenv("DB_PORT", "5432"))),
            dbname=secret.get("dbname", os.getenv("DB_NAME", "lead_assignment")),
            user=secret["username"],
            password=secret["password"],
        )

    # Local / env-var fallback.
    return DatabaseConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "lead_assignment"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
    )


def _resolve_lsq_key() -> str:
    secret_arn = os.getenv("LSQ_SECRET_ARN")
    if secret_arn:
        secret = _load_secret_json(secret_arn)
        return secret.get("api_key", "")
    return os.getenv("LSQ_API_KEY", "mock-lsq-api-key")


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached application configuration."""
    return AppConfig(
        database=_resolve_database_config(),
        model_bucket=os.getenv("MODEL_BUCKET", "lead-assignment-models-dev"),
        lsq_api_base_url=os.getenv("LSQ_API_BASE_URL", "https://mock-lsq.local/api"),
        lsq_api_key=_resolve_lsq_key(),
        # Same reasoning as _load_secret_json: env var wins, and the fallback is
        # Oregon because that is the only region the lab account can reach.
        aws_region=os.getenv("AWS_REGION", "us-west-2"),
        environment=os.getenv("ENVIRONMENT", "local"),
    )
