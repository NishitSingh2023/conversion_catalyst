terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# Container-image packaging. Zip packaging caps code + layers at 250MB unzipped;
# this project's dependencies are ~820MB (xgboost alone 417MB) and even the
# optimizer, which loads no model, reaches 249MB from scipy/pandas/numpy. Images
# raise the ceiling to 10GB. All stages share one image and select their
# entrypoint through image_config.command, so they cannot drift apart.
resource "aws_lambda_function" "this" {
  function_name = "${var.name_prefix}-${var.name}"
  role          = var.role_arn
  package_type  = "Image"
  image_uri     = var.image_uri
  timeout       = var.timeout
  memory_size   = var.memory_size

  image_config {
    command = [var.handler]
  }

  environment {
    variables = var.environment
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${aws_lambda_function.this.function_name}"
  retention_in_days = 14
  tags              = var.tags
}
