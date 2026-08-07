resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "models" {
  bucket        = "${local.name_prefix}-models-${random_id.bucket_suffix.hex}"
  force_destroy = true
  tags          = merge(local.common_tags, { Name = "${local.name_prefix}-models" })
}

resource "aws_s3_bucket_versioning" "models" {
  bucket = aws_s3_bucket.models.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "models" {
  bucket = aws_s3_bucket.models.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
