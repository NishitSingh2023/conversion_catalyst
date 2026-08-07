#!/usr/bin/env bash
# Build the pipeline container image and push it to ECR.
#
# One image serves every stage; each Lambda selects its entrypoint via the CMD
# override in Terraform. Run this before `terraform apply` (the functions
# reference an image tag that must already exist), and after any code change.
#
# Usage:
#   ./scripts/build_and_push.sh                 # tag: latest, region from AWS CLI
#   IMAGE_TAG=v2 AWS_REGION=ap-south-1 ./scripts/build_and_push.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT_NAME:-lead-assignment}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REGION="${AWS_REGION:-$(aws configure get region)}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

REPO_NAME="${PROJECT}-${ENVIRONMENT}-pipeline"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO_NAME}:${IMAGE_TAG}"

echo "==> Ensuring ECR repository ${REPO_NAME} exists"
aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO_NAME}" --region "${REGION}" >/dev/null

echo "==> Logging in to ${REGISTRY}"
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> Building ${IMAGE}"
# Lambda runs x86_64; force the platform so arm64 laptops produce a usable image.
docker build --platform linux/amd64 -t "${IMAGE}" "${REPO_ROOT}"

echo "==> Pushing ${IMAGE}"
docker push "${IMAGE}"

echo
echo "Image pushed. Deploy with:"
echo "  cd terraform && terraform apply -var=\"image_tag=${IMAGE_TAG}\""
