output "api_url" {
  description = "Put this in the shortcuts' Get Contents of URL action"
  value       = "${aws_apigatewayv2_api.api.api_endpoint}/translate"
}

output "api_secret" {
  description = "Value for the x-api-key header (terraform output -raw api_secret)"
  value       = random_password.api_secret.result
  sensitive   = true
}

output "ecr_repository_url" {
  value = aws_ecr_repository.push.repository_url
}

output "push_function" {
  value = aws_lambda_function.push.function_name
}

output "secret_parameters" {
  description = "Set these once with aws ssm put-parameter --overwrite"
  value = {
    deepl_key        = local.params.deepl_key
    ankiweb_user     = local.params.ankiweb_user
    ankiweb_password = local.params.ankiweb_password
  }
}
