# Nightly trigger for the assignment pipeline. Runs before the sales team's
# morning so leads are pre-assigned in LSQ.
resource "aws_cloudwatch_event_rule" "nightly_assignment" {
  name                = "${local.name_prefix}-nightly-assignment"
  description         = "Kick off the lead assignment Step Function each night."
  schedule_expression = var.assignment_schedule
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "nightly_assignment" {
  rule     = aws_cloudwatch_event_rule.nightly_assignment.name
  arn      = aws_sfn_state_machine.assignment.arn
  role_arn = aws_iam_role.events.arn

  # Seed the execution with a run context; the Ingest Lambda fills in the rest.
  input = jsonencode({
    trigger = "scheduled"
    source  = "eventbridge-nightly"
  })
}

# Weekly training schedule. Target wiring for the training job (SageMaker/Fargate)
# is added in the training task; the rule is defined here so the schedule exists.
resource "aws_cloudwatch_event_rule" "weekly_training" {
  name                = "${local.name_prefix}-weekly-training"
  description         = "Weekly model retraining trigger."
  schedule_expression = var.training_schedule
  tags                = local.common_tags
}
