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
  description = <<-DESC
    RDS allocated storage in GB. Storage can be grown in place but never shrunk,
    so this is sized with room rather than to fit.

    Measured locally against the real dataset (2.7M history rows, 30k leads per
    batch), after the eligibility shortlist cap:

      loaded history + indexes               482 MB   fixed, grows with history
      new_leads per batch                     16 MB
      eligibility_matrix per run             270 MB   1.49M rows @ ~181 B
      scores per run                         233 MB   1.31M rows @ ~179 B
      assignments + pool per run              ~15 MB
      ---------------------------------------------
      persisted per nightly run              ~518 MB
      temp files during eligibility          3.1 GB   transient; the two per-lead
                                                      window sorts over 11.6M rows
                                                      spill at 4MB work_mem

    Nothing prunes the per-run intermediates - each stage only clears its own
    run_id - so they accumulate at ~0.5 GB per night. 20 GB therefore projects to
    roughly a month of nightly runs before the disk fills, and less once the
    3.1 GB transient peak is left free. 100 GB projects to ~185 runs (about six
    months) on top of that peak.

    Two things would push this further out if it ever matters: a retention policy
    on eligibility_matrix/scores, which is the real fix, and more work_mem, which
    would keep the eligibility sorts in memory.
  DESC
  type        = number
  default     = 100
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
