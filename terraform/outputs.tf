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
  description = "Names of the pipeline and operational Lambdas."
  value = {
    ingest      = module.lambda_ingest.function_name
    eligibility = module.lambda_eligibility.function_name
    scoring     = module.lambda_scoring.function_name
    optimizer   = module.lambda_optimizer.function_name
    pool        = module.lambda_pool.function_name
    lsq_push    = module.lambda_lsq_push.function_name
    migrate     = module.lambda_migrate.function_name
    train       = module.lambda_train.function_name
  }
}

output "ecr_repository_url" {
  description = "Push the pipeline image here (see scripts/build_and_push.sh)."
  value       = aws_ecr_repository.pipeline.repository_url
}

output "migrate_command" {
  description = "Run this once after apply to create the schema in private RDS."
  value       = "aws lambda invoke --function-name ${module.lambda_migrate.function_name} --region ${var.aws_region} /dev/stdout"
}

# --- Bastion (null unless enable_bastion = true) ---------------------------

output "bastion_public_ip" {
  description = "Public IP of the SSH bastion, or null when the bastion is disabled."
  value       = var.enable_bastion ? aws_instance.bastion[0].public_ip : null
}

output "bastion_public_dns" {
  description = "Public DNS name of the SSH bastion, or null when the bastion is disabled."
  value       = var.enable_bastion ? aws_instance.bastion[0].public_dns : null
}

output "bastion_ssh_tunnel_command" {
  # Local port 5434, not 5433: docker-compose already publishes the local
  # Postgres on 5433, and a clash there fails as a confusing auth error against
  # the wrong database rather than as a bind error. Point DB_HOST=localhost and
  # DB_PORT=5434 at the tunnel for scripts/load_sample_data.py or the dashboard.
  description = "Background SSH local-forward from localhost:5434 to private RDS. Null when the bastion is disabled."
  value = var.enable_bastion ? join(" ", [
    "ssh -i ${trimsuffix(var.bastion_public_key_path, ".pub")}",
    "-o StrictHostKeyChecking=accept-new -f -N",
    "-L 5434:${aws_db_instance.postgres.address}:5432",
    "ec2-user@${aws_instance.bastion[0].public_ip}",
  ]) : null
}

output "bastion_db_password_command" {
  # Prints the password to your own terminal only; it is never a Terraform
  # output, so it stays out of state and out of CI logs.
  description = "Fetch the DB password from Secrets Manager for use through the tunnel."
  value = format(
    "aws secretsmanager get-secret-value --secret-id %s --region %s --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"password\"])'",
    aws_secretsmanager_secret.db.arn,
    var.aws_region,
  )
}
