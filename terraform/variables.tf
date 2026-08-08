variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  # us-west-2 (Oregon) is the only region the hackathon lab account is enabled
  # for. Calls made against any other region come back as opaque "access
  # denied" errors, which is the single most common failure there, so the
  # default points at Oregon rather than the AWS-wide default of us-east-1.
  default = "us-west-2"
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

variable "image_tag" {
  description = "Tag of the pipeline container image in ECR (built by scripts/build_and_push.sh)."
  type        = string
  default     = "latest"
}

variable "enable_nat_gateway" {
  description = <<-DESC
    Provision a NAT gateway so the lsq_push stage can reach the real LSQ API.
    Leave false while using the mock endpoint to avoid the hourly charge; set
    true before pointing lsq_api_base_url at a live sandbox, otherwise the push
    hangs until timeout.
  DESC
  type        = bool
  default     = false
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
variable "enable_bastion" {
  description = <<-DESC
    Provision a small SSH jump host in a public subnet. RDS is private and its
    security group only admits the Lambda SG, so nothing on a laptop can reach
    Postgres directly: not scripts/load_sample_data.py, and not the Streamlit
    dashboard, which talks to Postgres rather than going through a Lambda. The
    bastion exists purely so an SSH local-forward can bridge that gap.

    Off by default. It is an internet-facing instance, so stop it or destroy it
    (`-var="enable_bastion=false"` then apply) as soon as the load or the demo
    is finished rather than leaving it running.
  DESC
  type        = bool
  default     = false
}

variable "bastion_allowed_cidr" {
  description = <<-DESC
    The single CIDR allowed to SSH to the bastion. Must be a /32 - use your own
    public address (`curl -s https://checkip.amazonaws.com`), never 0.0.0.0/0.
    Home and mobile addresses rotate, so if SSH starts timing out after working
    earlier, re-check your address and re-apply with the new value before
    assuming anything is wrong with the host.
  DESC
  type        = string
  default     = ""

  validation {
    # Empty is allowed only because the bastion is off by default; the
    # enable_bastion-and-no-CIDR combination is rejected by the precondition on
    # the bastion security group in bastion.tf, which fails at plan time.
    condition     = var.bastion_allowed_cidr == "" || can(cidrnetmask(var.bastion_allowed_cidr)) && endswith(var.bastion_allowed_cidr, "/32")
    error_message = "bastion_allowed_cidr must be a single host CIDR ending in /32, e.g. 203.0.113.7/32."
  }
}

variable "bastion_instance_type" {
  description = "Instance type for the bastion. It only forwards TCP, so the smallest burstable size is plenty."
  type        = string
  default     = "t3.micro"
}

variable "bastion_public_key_path" {
  description = "Path to the local SSH public key installed on the bastion. Expanded with pathexpand, so ~ works."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}
