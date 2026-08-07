output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.main.id
}

output "db_endpoint" {
  description = "RDS Postgres endpoint (private)."
  value       = aws_db_instance.postgres.address
}

output "db_secret_arn" {
  description = "Secrets Manager ARN holding DB credentials."
  value       = aws_secretsmanager_secret.db.arn
}

output "lsq_secret_arn" {
  description = "Secrets Manager ARN holding the LSQ API key."
  value       = aws_secretsmanager_secret.lsq.arn
}

output "model_bucket" {
  description = "S3 bucket for model artifacts."
  value       = aws_s3_bucket.models.bucket
}

output "state_machine_arn" {
  description = "Assignment pipeline Step Function ARN."
  value       = aws_sfn_state_machine.assignment.arn
}

output "lambda_function_names" {
  description = "Names of the pipeline Lambdas."
  value = {
    ingest      = module.lambda_ingest.function_name
    eligibility = module.lambda_eligibility.function_name
    scoring     = module.lambda_scoring.function_name
    optimizer   = module.lambda_optimizer.function_name
    pool        = module.lambda_pool.function_name
    lsq_push    = module.lambda_lsq_push.function_name
  }
}
