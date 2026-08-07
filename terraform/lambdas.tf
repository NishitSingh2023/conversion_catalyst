locals {
  # Every function runs the same image; image_config.command selects the stage.
  image_uri = "${aws_ecr_repository.pipeline.repository_url}:${var.image_tag}"
}

# --- Pipeline stages -------------------------------------------------------
module "lambda_ingest" {
  source             = "./modules/lambda_function"
  name               = "ingest"
  name_prefix        = local.name_prefix
  image_uri          = local.image_uri
  handler            = "lambdas.ingest.handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_eligibility" {
  source      = "./modules/lambda_function"
  name        = "eligibility"
  name_prefix = local.name_prefix
  image_uri   = local.image_uri
  handler     = "lambdas.eligibility.handler.lambda_handler"
  # The cross join and filtering run inside Postgres, so this stage streams
  # counts rather than rows and needs little memory.
  memory_size        = 1024
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_scoring" {
  source      = "./modules/lambda_function"
  name        = "scoring"
  name_prefix = local.name_prefix
  image_uri   = local.image_uri
  handler     = "lambdas.scoring.handler.lambda_handler"
  # Holds the model plus the eligible-pair feature matrix.
  memory_size        = 3008
  timeout            = 600
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_optimizer" {
  source      = "./modules/lambda_function"
  name        = "optimizer"
  name_prefix = local.name_prefix
  image_uri   = local.image_uri
  handler     = "lambdas.optimizer.handler.lambda_handler"
  # The assignment problem is the most memory-hungry stage at 600 managers.
  memory_size        = 4096
  timeout            = 900
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_pool" {
  source             = "./modules/lambda_function"
  name               = "pool"
  name_prefix        = local.name_prefix
  image_uri          = local.image_uri
  handler            = "lambdas.pool.handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_lsq_push" {
  source      = "./modules/lambda_function"
  name        = "lsq-push"
  name_prefix = local.name_prefix
  image_uri   = local.image_uri
  handler     = "lambdas.lsq_push.handler.lambda_handler"
  # Outbound HTTPS to LSQ with retries and rate limiting.
  timeout            = 600
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

# --- Operational functions -------------------------------------------------
# RDS is private with no bastion, so migrations must be applied from inside the
# VPC. This function carries db_migrations/ in the image and is the supported
# path for creating the schema after apply.
module "lambda_migrate" {
  source             = "./modules/lambda_function"
  name               = "migrate"
  name_prefix        = local.name_prefix
  image_uri          = local.image_uri
  handler            = "lambdas.migrate.handler.lambda_handler"
  timeout            = 300
  memory_size        = 512
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

# Training shares the image, so it uses the identical shared/ feature code as
# serving. Container packaging allows the memory this needs; if the real dataset
# outgrows a 15-minute Lambda this same entrypoint lifts onto Fargate unchanged.
module "lambda_train" {
  source             = "./modules/lambda_function"
  name               = "train"
  name_prefix        = local.name_prefix
  image_uri          = local.image_uri
  handler            = "training.handler.lambda_handler"
  timeout            = 900
  memory_size        = 8192
  role_arn           = aws_iam_role.lambda.arn
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}
