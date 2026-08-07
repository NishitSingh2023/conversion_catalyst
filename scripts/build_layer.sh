#!/usr/bin/env bash
# Build the two Lambda layers that Terraform expects:
#   1. build/python-deps-layer.zip  - third-party dependencies (requirements.txt)
#   2. build/shared-layer/          - the shared/ business-logic library
#
# Both follow the Lambda layer convention of a top-level python/ directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/terraform/build"
DEPS_DIR="${BUILD_DIR}/deps/python"
SHARED_DIR="${BUILD_DIR}/shared-layer/python"

echo "==> Cleaning build dir"
rm -rf "${BUILD_DIR}/deps" "${BUILD_DIR}/shared-layer" "${BUILD_DIR}/python-deps-layer.zip"
mkdir -p "${DEPS_DIR}" "${SHARED_DIR}"

echo "==> Installing dependencies into layer (manylinux wheels for Lambda)"
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target "${DEPS_DIR}" \
  -r "${REPO_ROOT}/requirements.txt"

echo "==> Packaging deps layer zip"
(cd "${BUILD_DIR}/deps" && zip -qr "${BUILD_DIR}/python-deps-layer.zip" python)

echo "==> Copying shared library into shared layer"
cp -r "${REPO_ROOT}/shared" "${SHARED_DIR}/shared"

echo "==> Done. Layers ready in ${BUILD_DIR}"
