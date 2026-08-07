locals {
  lambda_layers = [aws_lambda_layer_version.deps.arn, aws_lambda_layer_version.shared.arn]
}

module "lambda_ingest" {
  source             = "./modules/lambda_function"
  name               = "ingest"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/ingest"
  handler            = "handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_eligibility" {
  source             = "./modules/lambda_function"
  name               = "eligibility"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/eligibility"
  handler            = "handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_scoring" {
  source             = "./modules/lambda_function"
  name               = "scoring"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/scoring"
  handler            = "handler.lambda_handler"
  memory_size        = 2048
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_optimizer" {
  source             = "./modules/lambda_function"
  name               = "optimizer"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/optimizer"
  handler            = "handler.lambda_handler"
  memory_size        = 2048
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_pool" {
  source             = "./modules/lambda_function"
  name               = "pool"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/pool"
  handler            = "handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}

module "lambda_lsq_push" {
  source             = "./modules/lambda_function"
  name               = "lsq-push"
  name_prefix        = local.name_prefix
  source_dir         = "${path.root}/../lambdas/lsq_push"
  handler            = "handler.lambda_handler"
  role_arn           = aws_iam_role.lambda.arn
  layer_arns         = local.lambda_layers
  environment        = local.lambda_env
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.lambda.id]
  tags               = local.common_tags
}
