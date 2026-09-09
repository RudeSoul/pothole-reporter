from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "terraform"
MAIN = (ROOT / "main.tf").read_text(encoding="utf-8")
OUTPUTS = (ROOT / "outputs.tf").read_text(encoding="utf-8")
VARIABLES = (ROOT / "variables.tf").read_text(encoding="utf-8")
TFVARS = (ROOT / "terraform.tfvars.example").read_text(encoding="utf-8")
README = (ROOT.parent / "README.md").read_text(encoding="utf-8")


class TerraformContractTests(unittest.TestCase):
    def test_inference_is_not_exposed_before_an_image_uri_exists(self):
        self.assertIn('deploy_service                     = var.container_image_uri != ""', MAIN)
        self.assertIn("count = local.deploy_service ? 1 : 0", MAIN)

    def test_both_monthly_caps_reach_lambda_as_integer_microusd(self):
        self.assertIn("MONTHLY_REQUEST_CAP", MAIN)
        self.assertIn("MONTHLY_ESTIMATED_MICROUSD_CAP", MAIN)
        self.assertIn("ESTIMATED_MICROUSD_PER_REQUEST", MAIN)
        self.assertIn("floor(var.monthly_estimated_budget_usd * 1000000)", MAIN)
        self.assertIn("ceil(var.estimated_cost_per_request_usd * 1000000)", MAIN)

    def test_plaintext_gateway_secret_is_not_in_infrastructure(self):
        self.assertIn("API_KEY_SHA256", MAIN)
        self.assertNotIn("YOLO_API_KEY", MAIN + VARIABLES)
        self.assertIn("sensitive   = true", VARIABLES)

    def test_lambda_has_bounded_concurrency_and_no_provisioned_concurrency(self):
        self.assertIn("reserved_concurrent_executions = var.reserved_concurrency", MAIN)
        self.assertNotIn("provisioned_concurrent", MAIN)

    def test_detector_route_requires_iam_before_lambda_invocation(self):
        route = MAIN.split(
            'resource "aws_apigatewayv2_route" "detector"', 1
        )[1].split('\nresource "', 1)[0]
        self.assertIn('route_key          = "POST /v1/detect"', route)
        self.assertIn('authorization_type = "AWS_IAM"', route)
        self.assertIn('/$default/POST/v1/detect', MAIN)

    def test_external_caller_policy_is_exact_and_credentials_are_not_provisioned(self):
        self.assertIn('output "yolo_caller_invoke_arn"', OUTPUTS)
        self.assertIn('output "yolo_caller_policy_json"', OUTPUTS)
        self.assertIn('Action   = "execute-api:Invoke"', OUTPUTS)
        self.assertIn('/$default/POST/v1/detect', OUTPUTS)
        infrastructure = MAIN + VARIABLES + OUTPUTS
        self.assertNotIn('resource "aws_iam_user"', infrastructure)
        self.assertNotIn('resource "aws_iam_access_key"', infrastructure)

    def test_ecr_lifecycle_never_expires_deployed_release_images(self):
        lifecycle = MAIN.split(
            'resource "aws_ecr_lifecycle_policy" "service"', 1
        )[1].split('\nresource "', 1)[0]
        self.assertNotIn('tagStatus   = "any"', lifecycle)
        self.assertIn('tagStatus   = "untagged"', lifecycle)
        self.assertNotIn('tagStatus      = "tagged"', lifecycle)
        self.assertNotIn('tagPatternList', lifecycle)
        self.assertIn('ECR evaluates expiry against an image', lifecycle)
        self.assertIn('deployed and release tags are never matched', README)
        self.assertRegex(README, r'Remove a rejected candidate\s+tag')

    def test_api_timeout_has_strict_headroom_over_lambda(self):
        self.assertRegex(
            VARIABLES,
            r'variable "lambda_timeout_seconds"[\s\S]*?default\s*=\s*27',
        )
        self.assertRegex(
            VARIABLES,
            r'variable "api_integration_timeout_seconds"[\s\S]*?default\s*=\s*29',
        )
        self.assertIn(
            "var.api_integration_timeout_seconds >= 2 && var.api_integration_timeout_seconds <= 30",
            VARIABLES,
        )
        self.assertIn(
            "floor(var.api_integration_timeout_seconds) == var.api_integration_timeout_seconds",
            VARIABLES,
        )
        self.assertIn(
            "timeout_milliseconds   = var.api_integration_timeout_seconds * 1000",
            MAIN,
        )
        self.assertIn(
            "condition     = var.api_integration_timeout_seconds > var.lambda_timeout_seconds",
            MAIN,
        )
        self.assertIn('resource "terraform_data" "timeout_headroom"', MAIN)
        self.assertIn("depends_on = [terraform_data.timeout_headroom]", MAIN)
        self.assertNotIn(
            "timeout_milliseconds   = var.lambda_timeout_seconds * 1000",
            MAIN,
        )

    def test_example_copies_the_evaluated_detection_threshold(self):
        self.assertRegex(
            TFVARS,
            r"detection_confidence_threshold\s*=\s*0\.50",
        )
        self.assertIn("decision.confidence_threshold", README)
        self.assertNotIn("clear_confidence", MAIN + VARIABLES + TFVARS + README)

    def test_model_version_accepts_the_release_pipeline_length_contract(self):
        model_version_variable = VARIABLES.split(
            'variable "model_version"', 1
        )[1].split('\nvariable "', 1)[0]
        self.assertIn("length(var.model_version) >= 1", model_version_variable)
        self.assertIn("length(var.model_version) <= 80", model_version_variable)
        self.assertNotIn("length(var.model_version) >= 3", model_version_variable)
        self.assertIn("1-80 characters", model_version_variable)

    def test_role_cannot_scan_or_write_arbitrary_dynamodb_tables(self):
        self.assertIn('["dynamodb:GetItem", "dynamodb:UpdateItem"]', MAIN)
        self.assertIn("resources = [aws_dynamodb_table.monthly_admission.arn]", MAIN)
        self.assertNotIn('dynamodb:*', MAIN)

    def test_lambda_has_the_documented_minimum_ecr_pull_policy(self):
        self.assertIn('sid    = "LambdaECRImageRetrievalPolicy"', MAIN)
        self.assertIn('"ecr:BatchGetImage"', MAIN)
        self.assertIn('"ecr:GetDownloadUrlForLayer"', MAIN)
        self.assertIn('identifiers = ["lambda.amazonaws.com"]', MAIN)

    def test_stack_does_not_persist_images(self):
        self.assertNotIn("aws_s3_", MAIN)
        self.assertNotIn("request.body", MAIN.lower())


if __name__ == "__main__":
    unittest.main()
