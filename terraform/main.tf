data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name_prefix = "${var.project_name}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Two private subnets (RDS + Lambda ENIs) derived from the VPC CIDR.
  private_subnet_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, i)]

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
  }

  # Environment variables shared by every Lambda so they can reach Postgres,
  # S3 and Secrets Manager without hardcoded config.
  lambda_env = {
    ENVIRONMENT      = var.environment
    DB_SECRET_ARN    = aws_secretsmanager_secret.db.arn
    LSQ_SECRET_ARN   = aws_secretsmanager_secret.lsq.arn
    DB_HOST          = aws_db_instance.postgres.address
    DB_PORT          = tostring(aws_db_instance.postgres.port)
    DB_NAME          = var.db_name
    MODEL_BUCKET     = aws_s3_bucket.models.bucket
    LSQ_API_BASE_URL = var.lsq_api_base_url
    AWS_REGION       = var.aws_region
  }
}
