variable "region" {
  description = "Same region as the Atlas host, so the heartbeat path stays short."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "AWS CLI profile for the separate Atlas account. null = default credentials."
  type        = string
  default     = null
}

variable "project" {
  type    = string
  default = "atlas-watchtower"
}

variable "alert_email" {
  description = "Where alerts and the daily report go. Confirm the SNS email after the first apply."
  type        = string
}

variable "telegram_bot_token" {
  description = "Optional. From @BotFather. Leave empty for email-only alerts."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  description = "Optional. The chat (or group) the bot should post into."
  type        = string
  default     = ""
}

variable "heartbeat_timeout_min" {
  description = "Minutes of silence during market hours before the DOWN alert."
  type        = number
  default     = 5
}

variable "stale_min" {
  description = "Minutes since the last market tick before the STALE DATA alert."
  type        = number
  default     = 3
}

variable "daily_loss_alert" {
  description = "Alert when day P&L drops below minus this many dollars. Set it under the prop firm's own limit. 0 = off."
  type        = number
  default     = 0
}

variable "drawdown_alert" {
  description = "Warn when the account is this many dollars below its best balance (Lucid Flex closes at $1,000). 0 = off."
  type        = number
  default     = 750
}

variable "account_start" {
  description = "Prop account starting balance, for the console's account panel (e.g. 50000). 0 = hide target progress."
  type        = number
  default     = 0
}

variable "account_max_drawdown" {
  description = "The firm's trailing drawdown limit in dollars (Lucid Flex: 1000). The console shows how much room is left. 0 = off."
  type        = number
  default     = 1000
}

variable "account_profit_target" {
  description = "Profit target to pass the evaluation, in dollars. 0 = off."
  type        = number
  default     = 0
}

variable "monthly_budget_usd" {
  type    = number
  default = 5
}
