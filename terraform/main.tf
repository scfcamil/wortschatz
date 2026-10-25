terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { project = var.name } }
}

locals {
  param_prefix = "/${var.name}"
  params = {
    api_secret       = "${local.param_prefix}/api_secret"
    deepl_key        = "${local.param_prefix}/deepl_key"
    ankiweb_user     = "${local.param_prefix}/ankiweb_user"
    ankiweb_password = "${local.param_prefix}/ankiweb_password"
  }
}

# ---------------------------------------------------------------- secrets (SSM, free standard tier)

resource "random_password" "api_secret" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "api_secret" {
  name  = local.params.api_secret
  type  = "SecureString"
  value = random_password.api_secret.result
}

# real values are set once with `aws ssm put-parameter --overwrite` (see README) so they never land in state
resource "aws_ssm_parameter" "manual" {
  for_each = toset(["deepl_key", "ankiweb_user", "ankiweb_password"])
  name     = local.params[each.key]
  type     = "SecureString"
  value    = "CHANGE_ME"
  lifecycle { ignore_changes = [value] }
}

# ---------------------------------------------------------------- storage

resource "aws_dynamodb_table" "words" {
  name           = "${var.name}-words"
  billing_mode   = "PROVISIONED" # 1/1 stays inside the always-free 25 RCU/WCU
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "word"

  attribute {
    name = "word"
    type = "S"
  }
}

resource "aws_s3_bucket" "collection" {
  bucket_prefix = "${var.name}-collection-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "collection" {
  bucket                  = aws_s3_bucket.collection.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "collection" {
  bucket = aws_s3_bucket.collection.id
  versioning_configuration { status = "Enabled" } # cheap undo for the collection copy
}

resource "aws_s3_bucket_lifecycle_configuration" "collection" {
  bucket = aws_s3_bucket.collection.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 14 }
  }
}

resource "aws_ecr_repository" "push" {
  name                 = "${var.name}-push"
  force_delete         = true
  image_tag_mutability = "MUTABLE"
}

resource "aws_ecr_lifecycle_policy" "push" {
  repository = aws_ecr_repository.push.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep last 2 images"
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 2 }
    action       = { type = "expire" }
  }] })
}

data "aws_ecr_image" "push" {
  repository_name = aws_ecr_repository.push.name
  image_tag       = var.push_image_tag
}

# ---------------------------------------------------------------- IAM

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "capture" {
  name               = "${var.name}-capture"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "push" {
  name               = "${var.name}-push"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  for_each   = { capture = aws_iam_role.capture.name, push = aws_iam_role.push.name }
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "capture" {
  role = aws_iam_role.capture.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = aws_dynamodb_table.words.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
    aws_ssm_parameter.api_secret.arn, aws_ssm_parameter.manual["deepl_key"].arn] },
  ] })
}

resource "aws_iam_role_policy" "push" {
  role = aws_iam_role.push.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:Scan", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.words.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.collection.arn}/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.collection.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
    aws_ssm_parameter.manual["ankiweb_user"].arn, aws_ssm_parameter.manual["ankiweb_password"].arn] },
  ] })
}

# ---------------------------------------------------------------- Lambdas

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = toset(["capture", "push"])
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 14
}

data "archive_file" "capture" {
  type        = "zip"
  source_file = "${path.module}/../capture/handler.py"
  output_path = "${path.module}/.build/capture.zip"
}

resource "aws_lambda_function" "capture" {
  function_name    = "${var.name}-capture"
  role             = aws_iam_role.capture.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.capture.output_path
  source_code_hash = data.archive_file.capture.output_base64sha256
  memory_size      = 256
  timeout          = 15
  environment {
    variables = {
      TABLE            = aws_dynamodb_table.words.name
      API_SECRET_PARAM = local.params.api_secret
      DEEPL_KEY_PARAM  = local.params.deepl_key
      EN_VARIANT       = var.english_variant
    }
  }
  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "push" {
  function_name                  = "${var.name}-push"
  role                           = aws_iam_role.push.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.push.repository_url}@${data.aws_ecr_image.push.image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 1024
  timeout                        = 180
  reserved_concurrent_executions = var.push_reserved_concurrency
  environment {
    variables = {
      TABLE                  = aws_dynamodb_table.words.name
      BUCKET                 = aws_s3_bucket.collection.bucket
      DECK                   = var.anki_deck
      ANKIWEB_USER_PARAM     = local.params.ankiweb_user
      ANKIWEB_PASSWORD_PARAM = local.params.ankiweb_password
    }
  }
  depends_on = [aws_cloudwatch_log_group.fn]
}

# ---------------------------------------------------------------- HTTP API for the shortcut

resource "aws_apigatewayv2_api" "api" {
  name          = var.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "capture" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.capture.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "translate" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /translate"
  target    = "integrations/${aws_apigatewayv2_integration.capture.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5 # caps anyone hammering the URL
    throttling_rate_limit  = 2
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.capture.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*/translate"
}

# ---------------------------------------------------------------- schedule for the push Lambda

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.push.arn }
  ] })
}

resource "aws_scheduler_schedule" "push" {
  name                = "${var.name}-push"
  schedule_expression = var.push_schedule
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.push.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy { maximum_retry_attempts = 0 } # next scheduled run retries anyway
  }
}
