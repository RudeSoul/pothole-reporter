variable "aws_region" {
  description = "AWS region for the private YOLO service."
  type        = string
  default     = "ap-south-1"
}

variable "name_prefix" {
  description = "Lowercase prefix used for AWS resource names."
  type        = string
  default     = "pothole-reporter-yolo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,40}$", var.name_prefix))
    error_message = "name_prefix must be 3-41 lowercase letters, numbers, or hyphens."
  }
}

variable "container_image_uri" {
  description = "Immutable ECR image URI (preferably @sha256:...). Empty bootstraps ECR and the counter without exposing an API."
  type        = string
  default     = ""
}

variable "api_key_sha256" {
  description = "Lowercase SHA-256 of the strong bearer token stored in the central server secret store. The plaintext is never placed in Terraform."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.api_key_sha256))
    error_message = "api_key_sha256 must be a lowercase 64-character SHA-256 hex digest."
  }
}

variable "model_version" {
  description = "Immutable evaluated release version; must equal model-manifest.json."
  type        = string

  validation {
    condition     = length(var.model_version) >= 1 && length(var.model_version) <= 80
    error_message = "model_version must contain 1-80 characters."
  }
}

variable "monthly_request_cap" {
  description = "Hard maximum number of valid YOLO attempts admitted in one UTC month. Zero is a kill switch."
  type        = number

  validation {
    condition     = var.monthly_request_cap >= 0 && floor(var.monthly_request_cap) == var.monthly_request_cap
    error_message = "monthly_request_cap must be a non-negative integer."
  }
}

variable "monthly_estimated_budget_usd" {
  description = "Hard cap on the configured estimated inference cost admitted per UTC month. This is not an AWS-bill cutoff. Zero is a kill switch."
  type        = number

  validation {
    condition     = var.monthly_estimated_budget_usd >= 0
    error_message = "monthly_estimated_budget_usd must be non-negative."
  }
}

variable "estimated_cost_per_request_usd" {
  description = "Conservative operator-reviewed estimate charged to the admission counter for every admitted attempt."
  type        = number

  validation {
    condition     = var.estimated_cost_per_request_usd > 0
    error_message = "estimated_cost_per_request_usd must be greater than zero."
  }
}

variable "lambda_architecture" {
  description = "Must match the architecture used for the Lambda container build."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.lambda_architecture)
    error_message = "lambda_architecture must be x86_64 or arm64."
  }
}

variable "lambda_memory_mb" {
  description = "CPU allocation scales with Lambda memory; benchmark before changing the per-request estimate."
  type        = number
  default     = 4096

  validation {
    condition     = var.lambda_memory_mb >= 1769 && var.lambda_memory_mb <= 10240 && floor(var.lambda_memory_mb) == var.lambda_memory_mb
    error_message = "lambda_memory_mb must be an integer between 1769 and 10240."
  }
}

variable "lambda_timeout_seconds" {
  description = "Lambda execution deadline. Must remain strictly below api_integration_timeout_seconds so the function can return its own response."
  type        = number
  default     = 27

  validation {
    condition     = var.lambda_timeout_seconds >= 2 && var.lambda_timeout_seconds <= 29 && floor(var.lambda_timeout_seconds) == var.lambda_timeout_seconds
    error_message = "lambda_timeout_seconds must be an integer from 2 through 29."
  }
}

variable "api_integration_timeout_seconds" {
  description = "API Gateway HTTP API integration deadline. Must be strictly greater than lambda_timeout_seconds and cannot exceed AWS's 30-second maximum."
  type        = number
  default     = 29

  validation {
    condition     = var.api_integration_timeout_seconds >= 2 && var.api_integration_timeout_seconds <= 30 && floor(var.api_integration_timeout_seconds) == var.api_integration_timeout_seconds
    error_message = "api_integration_timeout_seconds must be an integer from 2 through 30."
  }
}

variable "reserved_concurrency" {
  description = "Hard concurrent Lambda ceiling. Set zero for an immediate operator kill switch."
  type        = number
  default     = 2

  validation {
    condition     = var.reserved_concurrency >= 0 && var.reserved_concurrency <= 50 && floor(var.reserved_concurrency) == var.reserved_concurrency
    error_message = "reserved_concurrency must be an integer from 0 through 50."
  }
}

variable "api_throttle_rate" {
  description = "API Gateway steady-state requests per second. This controls bursts, not monthly spend."
  type        = number
  default     = 2

  validation {
    condition     = var.api_throttle_rate > 0 && var.api_throttle_rate <= 100
    error_message = "api_throttle_rate must be greater than zero and at most 100."
  }
}

variable "api_throttle_burst" {
  description = "API Gateway burst ceiling."
  type        = number
  default     = 4

  validation {
    condition     = var.api_throttle_burst >= 1 && var.api_throttle_burst <= 200 && floor(var.api_throttle_burst) == var.api_throttle_burst
    error_message = "api_throttle_burst must be an integer from 1 through 200."
  }
}

variable "detection_confidence_threshold" {
  description = "Threshold copied exactly from the evaluated model manifest; a mismatch fails closed."
  type        = number

  validation {
    condition     = var.detection_confidence_threshold >= 0.01 && var.detection_confidence_threshold <= 1
    error_message = "detection_confidence_threshold must be from 0.01 through 1."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Logs contain IDs/outcomes only, never request images."
  type        = number
  default     = 30

  validation {
    condition = contains([
      1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400,
      545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653
    ], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch-supported retention value."
  }
}

variable "budget_alert_email" {
  description = "Optional email for delayed, account-bill AWS Budget alerts. Null creates no AWS Budget."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.budget_alert_email == null || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be null or a plausible email address."
  }
}

variable "extra_tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
