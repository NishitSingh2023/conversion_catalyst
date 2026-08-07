variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment name (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Short project name used to prefix resource names."
  type        = string
  default     = "lead-assignment"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread subnets across."
  type        = number
  default     = 2
}

variable "db_name" {
  description = "PostgreSQL database name."
  type        = string
  default     = "lead_assignment"
}

variable "db_username" {
  description = "PostgreSQL master username."
  type        = string
  default     = "leadadmin"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB."
  type        = number
  default     = 20
}

variable "lambda_layer_zip" {
  description = "Path to the pre-built Python dependency layer zip (built by scripts/build_layer.sh)."
  type        = string
  default     = "build/python-deps-layer.zip"
}

variable "shared_layer_dir" {
  description = "Directory containing the shared/ library packaged for a Lambda layer."
  type        = string
  default     = "build/shared-layer"
}

variable "lsq_api_base_url" {
  description = "Base URL for the LSQ bulk API (mock/sandbox for the hackathon)."
  type        = string
  default     = "https://mock-lsq.local/api"
}

variable "assignment_schedule" {
  description = "EventBridge cron for the nightly assignment run (UTC)."
  type        = string
  default     = "cron(30 22 * * ? *)" # 22:30 UTC ~= 04:00 IST
}

variable "training_schedule" {
  description = "EventBridge cron for the weekly model training run (UTC)."
  type        = string
  default     = "cron(0 20 ? * SUN *)" # Sundays 20:00 UTC
}
