data "archive_file" "src" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/build/watchtower.zip"
  excludes    = ["__pycache__"]
}

locals {
  table_arn   = aws_dynamodb_table.main.arn
  ledger_arn  = aws_s3_bucket.ledger.arn
  secret_arns = concat([aws_ssm_parameter.ingest_key.arn], aws_ssm_parameter.telegram_token[*].arn)

  # One code package, three jobs, three roles. Each role gets only what its job needs.
  functions = {
    api = {
      handler = "api.handler"
      timeout = 10
      statements = [
        { Sid = "State", Effect = "Allow", Action = ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:Scan", "dynamodb:Query"], Resource = local.table_arn },
        { Sid = "Ledger", Effect = "Allow", Action = ["s3:PutObject"], Resource = "${local.ledger_arn}/trades/*" },
        { Sid = "Key", Effect = "Allow", Action = ["ssm:GetParameter"], Resource = aws_ssm_parameter.ingest_key.arn },
      ]
    }
    watchdog = {
      handler = "watchdog.handler"
      timeout = 30
      statements = [
        { Sid = "State", Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"], Resource = local.table_arn },
        { Sid = "Alert", Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
        { Sid = "Telegram", Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.secret_arns },
      ]
    }
    report = {
      handler = "report.handler"
      timeout = 60
      statements = [
        { Sid = "Read", Effect = "Allow", Action = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:PutItem"], Resource = local.table_arn },
        { Sid = "Ledger", Effect = "Allow", Action = ["s3:PutObject"], Resource = "${local.ledger_arn}/reports/*" },
        { Sid = "Alert", Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
        { Sid = "Telegram", Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.secret_arns },
      ]
    }
  }
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.project}-${each.key}"
  retention_in_days = 30
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${var.project}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = each.key
  role     = aws_iam_role.fn[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(each.value.statements, [
      { Sid = "OwnLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fn[each.key].arn}:*" },
    ])
  })
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${var.project}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  handler          = each.value.handler
  filename         = data.archive_file.src.output_path
  source_code_hash = data.archive_file.src.output_base64sha256
  timeout          = each.value.timeout
  memory_size      = 256

  environment {
    variables = {
      TABLE                 = aws_dynamodb_table.main.name
      BUCKET                = aws_s3_bucket.ledger.bucket
      TOPIC_ARN             = aws_sns_topic.alerts.arn
      INGEST_KEY_PARAM      = aws_ssm_parameter.ingest_key.name
      TELEGRAM_TOKEN_PARAM  = var.telegram_bot_token == "" ? "" : aws_ssm_parameter.telegram_token[0].name
      TELEGRAM_CHAT_ID      = var.telegram_chat_id
      HEARTBEAT_TIMEOUT_MIN = tostring(var.heartbeat_timeout_min)
      STALE_MIN             = tostring(var.stale_min)
      DAILY_LOSS_ALERT      = tostring(var.daily_loss_alert)
      DRAWDOWN_ALERT        = tostring(var.drawdown_alert)
      ACCOUNT_START         = tostring(var.account_start)
      ACCOUNT_MAX_DRAWDOWN  = tostring(var.account_max_drawdown)
      ACCOUNT_PROFIT_TARGET = tostring(var.account_profit_target)
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy.fn]
}
