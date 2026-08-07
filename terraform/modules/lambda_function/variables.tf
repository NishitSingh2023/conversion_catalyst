variable "name" {
  description = "Function name suffix (prefixed with project + environment)."
  type        = string
}

variable "name_prefix" {
  description = "Project + environment prefix, e.g. lead-assignment-dev."
  type        = string
}

variable "image_uri" {
  description = "ECR image URI (including tag) that all stages share."
  type        = string
}

variable "handler" {
  description = "Dotted path to the handler, passed as the image CMD override."
  type        = string
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
