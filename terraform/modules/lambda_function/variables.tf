variable "name" {
  description = "Function name suffix (prefixed with the project/environment)."
  type        = string
}

variable "name_prefix" {
  description = "Project + environment prefix, e.g. lead-assignment-dev."
  type        = string
}

variable "source_dir" {
  description = "Directory containing the Lambda handler source to zip."
  type        = string
}

variable "handler" {
  description = "Lambda handler entrypoint, e.g. handler.lambda_handler."
  type        = string
}

variable "runtime" {
  description = "Lambda runtime."
  type        = string
  default     = "python3.11"
}

variable "timeout" {
  description = "Function timeout in seconds."
  type        = number
  default     = 300
}

variable "memory_size" {
  description = "Function memory in MB."
  type        = number
  default     = 1024
}

variable "role_arn" {
  description = "IAM role ARN the function assumes."
  type        = string
}

variable "layer_arns" {
  description = "Lambda layer ARNs to attach (shared deps + code)."
  type        = list(string)
  default     = []
}

variable "environment" {
  description = "Environment variables passed to the function."
  type        = map(string)
  default     = {}
}

variable "subnet_ids" {
  description = "Private subnet IDs for VPC access to RDS."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for the function ENIs."
  type        = list(string)
}

variable "tags" {
  description = "Resource tags."
  type        = map(string)
  default     = {}
}
