# The key Atlas sends in x-atlas-key. Generated here, never typed by a human.
# Read it with the command in `terraform output key_command`.
resource "random_password" "ingest_key" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "ingest_key" {
  name  = "/${var.project}/ingest-key"
  type  = "SecureString"
  value = random_password.ingest_key.result
}

resource "aws_ssm_parameter" "telegram_token" {
  count = var.telegram_bot_token == "" ? 0 : 1
  name  = "/${var.project}/telegram-token"
  type  = "SecureString"
  value = var.telegram_bot_token
}
