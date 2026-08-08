# --- Optional SSH bastion --------------------------------------------------
# A jump host for the two things that have to talk to Postgres from outside AWS:
# loading the sample CSVs (data/sample/ is in .dockerignore, so the Lambda image
# carries none) and running the Streamlit dashboard, which connects straight to
# Postgres. RDS is private with publicly_accessible = false, so both go through
# an SSH local-forward instead of exposing the database.
#
# Everything here is count-gated on enable_bastion and disappears when it is
# false. See `terraform output bastion_ssh_tunnel_command` after an apply.

resource "aws_key_pair" "bastion" {
  count      = var.enable_bastion ? 1 : 0
  key_name   = "${local.name_prefix}-bastion"
  public_key = file(pathexpand(var.bastion_public_key_path))
  tags       = merge(local.common_tags, { Name = "${local.name_prefix}-bastion-key" })
}

# Resolved from SSM rather than hardcoded: AMI ids are region-specific, so a
# literal would silently break the moment aws_region changes.
data "aws_ssm_parameter" "bastion_ami" {
  count = var.enable_bastion ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "bastion" {
  count       = var.enable_bastion ? 1 : 0
  name        = "${local.name_prefix}-bastion-sg"
  description = "SSH bastion; inbound SSH from one operator address only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from the single allowed operator CIDR."
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.bastion_allowed_cidr]
  }

  egress {
    description = "All outbound (RDS, package repos for the psql client)."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${local.name_prefix}-bastion-sg" })

  lifecycle {
    # Fail the plan rather than build an instance whose SG would be malformed or
    # wide open. Expressed as a precondition, not a variable validation block,
    # because it spans two variables and cross-variable validation needs
    # Terraform >= 1.9 while versions.tf still allows 1.5.
    precondition {
      condition     = var.bastion_allowed_cidr != ""
      error_message = "enable_bastion = true requires bastion_allowed_cidr, e.g. -var=\"bastion_allowed_cidr=$(curl -s https://checkip.amazonaws.com)/32\". It is deliberately not defaulted, so there is no path to an SSH port open to the internet."
    }
  }
}

resource "aws_instance" "bastion" {
  count                       = var.enable_bastion ? 1 : 0
  ami                         = data.aws_ssm_parameter.bastion_ami[0].value
  instance_type               = var.bastion_instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.bastion[0].id]
  key_name                    = aws_key_pair.bastion[0].key_name
  associate_public_ip_address = true

  root_block_device {
    encrypted   = true
    volume_size = 8
    volume_type = "gp3"
  }

  # IMDSv2 only. This host has a public IP, and IMDSv1's unauthenticated
  # request path is what turns an SSRF or a stray curl into credential theft.
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  # psql on the host makes the "is it the tunnel or is it the database" question
  # answerable from one hop closer to RDS. AL2023 ships the client as
  # postgresql16 in the default repos.
  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    dnf install -y postgresql16
  EOT

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-bastion"
    Purpose = "SSH jump host for private RDS access; stop or destroy when idle"
  })
}
