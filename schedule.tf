data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction",
    Resource = [aws_lambda_function.fn["watchdog"].arn, aws_lambda_function.fn["report"].arn] }]
  })
}

# The dead-man switch. ~43k runs a month, well inside Lambda's free tier.
resource "aws_scheduler_schedule" "watchdog" {
  name                = "${var.project}-watchdog"
  schedule_expression = "rate(1 minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.fn["watchdog"].arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 0 # next minute's run is the retry
    }
  }
}

# After the CME close, on New York time (handles daylight saving on its own).
resource "aws_scheduler_schedule" "report" {
  name                         = "${var.project}-daily-report"
  schedule_expression          = "cron(10 17 ? * MON-FRI *)"
  schedule_expression_timezone = "America/New_York"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.fn["report"].arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
