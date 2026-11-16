output "watchtower_url" {
  description = "Give this to Atlas as WATCHTOWER_URL."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "key_command" {
  description = "Run this to print the key Atlas needs as WATCHTOWER_KEY. Never paste it in chat or a repo."
  value       = "aws ssm get-parameter --name ${aws_ssm_parameter.ingest_key.name} --with-decryption --query Parameter.Value --output text${var.aws_profile == null ? "" : " --profile ${var.aws_profile}"} --region ${var.region}"
}

output "ledger_bucket" {
  value = aws_s3_bucket.ledger.bucket
}
