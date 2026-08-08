#!/usr/bin/env bash
# Build the pipeline container image and push it to ECR.
#
# One image serves every stage; each Lambda selects its entrypoint via the CMD
# override in Terraform. Run this before the main `terraform apply` (the
# functions reference an image tag that must already exist), and after any code
# change.
#
# The ECR repository is a Terraform-managed resource, so create it with
# `terraform apply -target=aws_ecr_repository.pipeline` first and let the
# describe below find it. If this script creates the repository instead, the
# later full apply fails with RepositoryAlreadyExistsException because the
# repository exists in AWS but not in Terraform state. See README.
#
# Usage:
#   ./scripts/build_and_push.sh                 # tag: latest, region us-west-2
#   IMAGE_TAG=v2 AWS_REGION=ap-south-1 ./scripts/build_and_push.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT_NAME:-lead-assignment}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
# Must match terraform's aws_region (default us-west-2), because a Lambda can
# only run an image from an ECR repository in its own region. Falling back to
# `aws configure get region` meant a laptop configured for us-east-1 pushed the
# image there while terraform built the repo in us-west-2, and apply then failed
# on an image it could not find. AWS_REGION still overrides.
REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

REPO_NAME="${PROJECT}-${ENVIRONMENT}-pipeline"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPO_NAME}:${IMAGE_TAG}"

echo "==> Ensuring ECR repository ${REPO_NAME} exists"
aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO_NAME}" --region "${REGION}" >/dev/null

echo "==> Logging in to ${REGISTRY}"
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${REGISTRY}"

# buildx carries the flags below that keep the image in a shape Lambda accepts.
# Plain `docker build` on an older daemon without the plugin cannot express them,
# so stop here rather than push an image the functions will reject.
if ! docker buildx version >/dev/null 2>&1; then
  echo "ERROR: docker buildx is not available." >&2
  echo "Upgrade Docker (buildx ships with Docker 23+) or install the buildx plugin," >&2
  echo "e.g. 'apt install docker-buildx-plugin', then re-run this script." >&2
  exit 1
fi

echo "==> Building and pushing ${IMAGE}"
# Lambda runs x86_64; force the platform so arm64 laptops produce a usable image.
#
# The three extra flags exist because Docker 29 / buildx 0.35 defaults produce an
# artifact Lambda refuses. By default buildx publishes an OCI image index holding
# a provenance/SBOM attestation manifest next to the real image, while Lambda
# accepts only a single-platform Docker Image Manifest V2 Schema 2 with no
# attestations. Getting this wrong fails CreateFunction/UpdateFunctionCode for
# every function with "InvalidParameterValueException: The image manifest, config
# or layer media type for the source image is not supported", which reads like a
# broken image rather than a packaging default.
#   --provenance=false --sbom=false  drop the attestation manifest, so the index
#                                    has nothing to wrap
#   oci-mediatypes=false             emit Docker v2 schema 2 media types instead
#                                    of the OCI equivalents
# push=true publishes from the same invocation, so there is no separate
# `docker push` that could re-wrap the result.
docker buildx build --platform linux/amd64 \
  --provenance=false --sbom=false \
  --output "type=image,name=${IMAGE},oci-mediatypes=false,push=true" \
  "${REPO_ROOT}"

echo "==> Verifying pushed manifest media type"
# Read the manifest back out of ECR rather than trusting the build flags. This is
# the guard against the failure above reappearing silently after a Docker or
# buildx upgrade changes a default: better to fail here than during apply.
EXPECTED_MEDIA_TYPE="application/vnd.docker.distribution.manifest.v2+json"
MANIFEST="$(aws ecr batch-get-image \
  --repository-name "${REPO_NAME}" \
  --image-ids imageTag="${IMAGE_TAG}" \
  --region "${REGION}" \
  --query 'images[0].imageManifest' \
  --output text)"
# ECR hands back the manifest pretty-printed, so the top-level mediaType is the
# one indented by two spaces; the deeper matches are the config and layer types.
# The unanchored match is the fallback for a compact manifest, where the first
# mediaType is still the manifest's own (an OCI index reports its index type
# there, which is exactly what this check needs to see).
ACTUAL_MEDIA_TYPE="$(printf '%s\n' "${MANIFEST}" \
  | grep -o '^  "mediaType"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | head -n 1 \
  | sed 's/.*"\([^"]*\)"$/\1/')"
if [ -z "${ACTUAL_MEDIA_TYPE}" ]; then
  ACTUAL_MEDIA_TYPE="$(printf '%s\n' "${MANIFEST}" \
    | grep -o '"mediaType"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | head -n 1 \
    | sed 's/.*"\([^"]*\)"$/\1/')"
fi

if [ "${ACTUAL_MEDIA_TYPE}" != "${EXPECTED_MEDIA_TYPE}" ]; then
  echo "ERROR: ${IMAGE} has manifest media type '${ACTUAL_MEDIA_TYPE}'," >&2
  echo "       expected '${EXPECTED_MEDIA_TYPE}'." >&2
  echo "Lambda will reject this image with InvalidParameterValueException." >&2
  echo "An OCI image index means the attestation/OCI-media-type flags above did" >&2
  echo "not take effect; check the docker and buildx versions in use." >&2
  exit 1
fi
echo "    ${ACTUAL_MEDIA_TYPE}"

echo
echo "Image pushed. Deploy with:"
echo "  cd terraform && terraform apply -var=\"image_tag=${IMAGE_TAG}\""
