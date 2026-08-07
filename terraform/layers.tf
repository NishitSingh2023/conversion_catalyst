# Heavy third-party dependencies (xgboost, scipy, pandas, sqlalchemy, psycopg2...)
# are shipped as one layer built by scripts/build_layer.sh. Kept separate from the
# function code so redeploys of business logic stay fast.
resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${local.name_prefix}-deps"
  filename            = var.lambda_layer_zip
  compatible_runtimes = ["python3.11"]
  description         = "Python dependencies for the lead assignment pipeline."
}

# The shared/ library packaged as its own layer so every function imports the
# exact same feature-engineering and DB code (no train/serve skew).
data "archive_file" "shared_layer" {
  type        = "zip"
  source_dir  = var.shared_layer_dir
  output_path = "${path.root}/build/shared-layer.zip"
}

resource "aws_lambda_layer_version" "shared" {
  layer_name          = "${local.name_prefix}-shared"
  filename            = data.archive_file.shared_layer.output_path
  source_code_hash    = data.archive_file.shared_layer.output_base64sha256
  compatible_runtimes = ["python3.11"]
  description         = "Shared business-logic library (shared/)."
}
