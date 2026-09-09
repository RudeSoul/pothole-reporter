locals {
  deploy_service                     = var.container_image_uri != ""
  monthly_estimated_microusd_cap     = floor(var.monthly_estimated_budget_usd * 1000000)
  estimated_microusd_per_request     = ceil(var.estimated_cost_per_request_usd * 1000000)
  tags = merge({
    Application = "PotholeReporterYolo"
    ManagedBy   = "Terraform"
  }, var.extra_tags)
}

# Terraform 1.6 variable validation cannot compare sibling input variables.
# Keep the cross-variable contract in an always-present resource so even the
# ECR-only bootstrap plan rejects a timeout pair without strict headroom.
resource "terraform_data" "timeout_headroom" {
  input = {
    lambda_seconds      = var.lambda_timeout_seconds
    integration_seconds = var.api_integration_timeout_seconds
  }

  lifecycle {
    precondition {
      condition     = var.api_integration_timeout_seconds > var.lambda_timeout_seconds
      error_message = "api_integration_timeout_seconds must be strictly greater than lambda_timeout_seconds."
    }
  }
}

resource "aws_ecr_repository" "service" {
  name                 = var.name_prefix
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "service" {
  repository = aws_ecr_repository.service.name
  policy = jsonencode({
    # ECR evaluates expiry against an image, not one selected tag. A digest with
    # both candidate and release tags could therefore lose its release tag too.
    # Only untagged images are safe to expire; rejected candidates enter this set
    # after their candidate tag is removed, while deployed release tags remain.
    rules = [{
      rulePriority = 1
      description  = "Retain the ten newest untagged or rejected candidate images"
      selection = {
        tagStatus   = "untagged"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "ecr_lambda_pull" {
  statement {
    sid    = "LambdaECRImageRetrievalPolicy"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    resources = [aws_ecr_repository.service.arn]
  }
}

resource "aws_ecr_repository_policy" "lambda_pull" {
  repository = aws_ecr_repository.service.name
  policy     = data.aws_iam_policy_document.ecr_lambda_pull.json
}

resource "aws_dynamodb_table" "monthly_admission" {
  name         = "${var.name_prefix}-monthly-admission"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "period"

  attribute {
    name = "period"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "api" {
  count             = local.deploy_service ? 1 : 0
  name              = "/aws/apigateway/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "AtomicMonthlyAdmissionOnly"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.monthly_admission.arn]
  }

  statement {
    sid       = "WriteStructuredLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name_prefix}-least-privilege"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_lambda_function" "detector" {
  count = local.deploy_service ? 1 : 0

  function_name = var.name_prefix
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.container_image_uri
  architectures = [var.lambda_architecture]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_seconds

  # This is a maximum, not pre-warmed/provisioned capacity. Zero is a kill switch.
  reserved_concurrent_executions = var.reserved_concurrency

  ephemeral_storage {
    size = 512
  }

  environment {
    variables = {
      API_KEY_SHA256                    = var.api_key_sha256
      BUDGET_TABLE                      = aws_dynamodb_table.monthly_admission.name
      MONTHLY_REQUEST_CAP               = tostring(var.monthly_request_cap)
      MONTHLY_ESTIMATED_MICROUSD_CAP    = tostring(local.monthly_estimated_microusd_cap)
      ESTIMATED_MICROUSD_PER_REQUEST    = tostring(local.estimated_microusd_per_request)
      MODEL_PATH                        = "/opt/model/model.onnx"
      MODEL_MANIFEST_PATH               = "/opt/model/model-manifest.json"
      MODEL_PARITY_PATH                 = "/opt/model/runtime-parity.json"
      MODEL_VERSION                     = var.model_version
      DETECTION_CONFIDENCE_THRESHOLD    = tostring(var.detection_confidence_threshold)
      MAX_JSON_BODY_BYTES               = "5500000"
      MAX_IMAGE_BYTES                   = "3500000"
      MAX_DECODED_PIXELS                = "12000000"
      ORT_INTRA_OP_THREADS              = "1"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_ecr_repository_policy.lambda_pull,
    aws_iam_role_policy.lambda,
  ]
}

resource "aws_apigatewayv2_api" "detector" {
  count         = local.deploy_service ? 1 : 0
  name          = var.name_prefix
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "detector" {
  count = local.deploy_service ? 1 : 0

  api_id                 = aws_apigatewayv2_api.detector[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.detector[0].invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = var.api_integration_timeout_seconds * 1000

  depends_on = [terraform_data.timeout_headroom]
}

resource "aws_apigatewayv2_route" "detector" {
  count = local.deploy_service ? 1 : 0

  api_id             = aws_apigatewayv2_api.detector[0].id
  route_key          = "POST /v1/detect"
  target             = "integrations/${aws_apigatewayv2_integration.detector[0].id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "default" {
  count = local.deploy_service ? 1 : 0

  api_id      = aws_apigatewayv2_api.detector[0].id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.api_throttle_burst
    throttling_rate_limit  = var.api_throttle_rate
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api[0].arn
    format = jsonencode({
      request_id          = "$context.requestId"
      route_key           = "$context.routeKey"
      status              = "$context.status"
      response_latency_ms = "$context.responseLatency"
      integration_error   = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "api" {
  count = local.deploy_service ? 1 : 0

  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.detector[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.detector[0].execution_arn}/$default/POST/v1/detect"
}

# This is deliberately advisory. AWS billing data is delayed, and a Budget alert
# cannot provide the request-path cutoff that the DynamoDB admission transaction does.
# With no cost filter it also observes the whole account, so unrelated spend can alert
# early rather than allowing this safety signal to under-report.
resource "aws_budgets_budget" "account_advisory" {
  count = var.budget_alert_email == null ? 0 : 1

  name         = "${var.name_prefix}-account-advisory"
  budget_type  = "COST"
  limit_amount = max(var.monthly_estimated_budget_usd, 0.01)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
