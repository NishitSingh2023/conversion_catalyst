terraform {
  required_version = ">= 1.5"

  # Majors are pinned rather than left open (">= 5.0" happily resolves 6.x and
  # would resolve 7.x on release). .terraform.lock.hcl is gitignored, so the
  # constraint here is the only thing keeping two people deploying from this
  # repo on the same provider. 6.x is what this config has been initialised
  # against. hashicorp/archive was declared but never used - all packaging is
  # container images - so it is no longer requested.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "lead-assignment-engine"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
