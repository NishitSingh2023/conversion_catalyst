# --- Lambda execution role -------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name_prefix}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.common_tags
}

# VPC networking (ENI management) + CloudWatch Logs.
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "lambda_permissions" {
  statement {
    sid     = "ReadWriteModelBucket"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      aws_s3_bucket.models.arn,
      "${aws_s3_bucket.models.arn}/*",
    ]
  }

  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db.arn, aws_secretsmanager_secret.lsq.arn]
  }
}

resource "aws_iam_role_policy" "lambda_permissions" {
  name   = "${local.name_prefix}-lambda-permissions"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# --- Step Functions role ---------------------------------------------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.name_prefix}-sfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "sfn_permissions" {
  statement {
    sid     = "InvokePipelineLambdas"
    actions = ["lambda:InvokeFunction"]
    resources = [
      module.lambda_ingest.function_arn,
      module.lambda_eligibility.function_arn,
      module.lambda_scoring.function_arn,
      module.lambda_optimizer.function_arn,
      module.lambda_pool.function_arn,
      module.lambda_lsq_push.function_arn,
    ]
  }
}

resource "aws_iam_role_policy" "sfn_permissions" {
  name   = "${local.name_prefix}-sfn-permissions"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn_permissions.json
}

# --- EventBridge scheduler role -------------------------------------------
data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events" {
  name               = "${local.name_prefix}-events-role"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "events_permissions" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.assignment.arn]
  }
}

resource "aws_iam_role_policy" "events_permissions" {
  name   = "${local.name_prefix}-events-permissions"
  role   = aws_iam_role.events.id
  policy = data.aws_iam_policy_document.events_permissions.json
}
