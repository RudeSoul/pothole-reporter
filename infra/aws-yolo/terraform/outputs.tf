output "ecr_repository_url" {
  description = "Push the evaluated Lambda container here after the bootstrap apply."
  value       = aws_ecr_repository.service.repository_url
}

output "yolo_api_url" {
  description = "Set as YOLO_API_URL on the central Worker after the second apply."
  value = local.deploy_service ? "${aws_apigatewayv2_api.detector[0].api_endpoint}/v1/detect" : null
}

output "yolo_caller_invoke_arn" {
  description = "Attach execute-api:Invoke for only this ARN to the external Worker caller principal."
  value = local.deploy_service ? "${aws_apigatewayv2_api.detector[0].execution_arn}/$default/POST/v1/detect" : null
}

output "yolo_caller_policy_json" {
  description = "Least-privilege policy template for an externally managed Worker caller principal; this stack creates no caller or access key."
  value = local.deploy_service ? jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokePotholeYoloDetector"
      Effect   = "Allow"
      Action   = "execute-api:Invoke"
      Resource = "${aws_apigatewayv2_api.detector[0].execution_arn}/$default/POST/v1/detect"
    }]
  }) : null
}

output "server_detector_provider" {
  description = "Enable only after yolo_api_url and the matching plaintext bearer secret are installed."
  value       = "openai_then_http_yolo"
}

output "monthly_caps" {
  description = "Admission limits enforced transactionally before ONNX initialization/inference."
  value = {
    requests                  = var.monthly_request_cap
    estimated_budget_usd      = var.monthly_estimated_budget_usd
    estimated_cost_per_request = var.estimated_cost_per_request_usd
  }
}
