# Nightly assignment workflow. Each state invokes one pipeline Lambda and passes
# the run context (run_id, batch_id) forward. Training runs as a separate job.
resource "aws_sfn_state_machine" "assignment" {
  name     = "${local.name_prefix}-assignment"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "Lead assignment nightly pipeline"
    StartAt = "Ingest"
    States = {
      Ingest = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_ingest.function_arn
          "Payload.$"  = "$"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.ingest"
        Retry          = local.sfn_retry
        Next           = "Eligibility"
      }
      Eligibility = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_eligibility.function_arn
          "Payload.$"  = "$.ingest.body"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.eligibility"
        Retry          = local.sfn_retry
        Next           = "Scoring"
      }
      Scoring = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_scoring.function_arn
          "Payload.$"  = "$.eligibility.body"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.scoring"
        Retry          = local.sfn_retry
        Next           = "Optimize"
      }
      Optimize = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_optimizer.function_arn
          "Payload.$"  = "$.scoring.body"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.optimize"
        Retry          = local.sfn_retry
        Next           = "PoolAllocation"
      }
      PoolAllocation = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_pool.function_arn
          "Payload.$"  = "$.optimize.body"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.pool"
        Retry          = local.sfn_retry
        Next           = "PushToLSQ"
      }
      PushToLSQ = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = module.lambda_lsq_push.function_arn
          "Payload.$"  = "$.pool.body"
        }
        ResultSelector = { "body.$" = "$.Payload" }
        ResultPath     = "$.push"
        Retry          = local.sfn_retry
        End            = true
      }
    }
  })

  tags = local.common_tags
}

locals {
  # Retry only genuinely transient infrastructure faults.
  #
  # States.TaskFailed was previously included, which retried deterministic
  # business errors ("batch has no valid leads", a missing handler module) three
  # times with backoff. Those never succeed on retry and each attempt left
  # another failed row in pipeline_runs.
  sfn_retry = [
    {
      ErrorEquals = [
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
        "Lambda.Unknown",
      ]
      IntervalSeconds = 5
      MaxAttempts     = 3
      BackoffRate     = 2.0
    }
  ]
}
