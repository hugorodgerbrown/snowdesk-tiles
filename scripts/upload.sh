#!/usr/bin/env bash
# Publish the dist tree to the R2 bucket (SNOW-485).
#
# Uses the AWS CLI against R2's S3-compatible endpoint rather than wrangler:
# `wrangler r2 object put` caps out at 315 MB and the PMTiles archive is several
# GB, so multipart upload is required.
#
# Content-Type has to be set explicitly per asset class. R2 does not infer it,
# and a .pmtiles or .pbf served as the wrong type breaks the client reader.
# Cache-Control likewise — it is object metadata, fixed at upload time.
#
# Requires: awscli v2, and these in the environment (never committed):
#     CLOUDFLARE_ACCOUNT_ID
#     AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (an R2 API token pair)
#
#     ./scripts/upload.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID (Cloudflare dashboard > R2)}"
: "${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID from an R2 API token}"
: "${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY from an R2 API token}"

: "${R2_ENDPOINT:=https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com}"
# R2 has no meaningful region, but the AWS CLI insists on one being set.
: "${AWS_DEFAULT_REGION:=auto}"
export AWS_DEFAULT_REGION

s3() { aws s3 "$@" --endpoint-url "$R2_ENDPOINT" --no-progress; }

for required in "${DIST_DIR}/${PMTILES_NAME}" "${DIST_DIR}/styles/liberty"; do
    if [ ! -f "$required" ]; then
        echo "error: ${required} missing — run build-extract.sh and build.sh first" >&2
        exit 1
    fi
done

# Order matters. The style names the PMTiles archive, so everything it points
# at must already be live before the style itself is published; otherwise a
# client that fetches the new style mid-upload gets a 404 on the tiles.

echo "==> glyphs"
s3 sync "${DIST_DIR}/fonts" "s3://${R2_BUCKET}/fonts" \
    --content-type application/x-protobuf \
    --cache-control "$IMMUTABLE_CACHE"

echo "==> sprite atlases"
s3 sync "${DIST_DIR}/sprites" "s3://${R2_BUCKET}/sprites" \
    --exclude '*' --include '*.png' \
    --content-type image/png \
    --cache-control "$IMMUTABLE_CACHE"

echo "==> sprite indexes"
s3 sync "${DIST_DIR}/sprites" "s3://${R2_BUCKET}/sprites" \
    --exclude '*' --include '*.json' \
    --content-type application/json \
    --cache-control "$IMMUTABLE_CACHE"

echo "==> natural earth raster tiles"
if [ -d "${DIST_DIR}/natural_earth" ]; then
    s3 sync "${DIST_DIR}/natural_earth" "s3://${R2_BUCKET}/natural_earth" \
        --content-type image/png \
        --cache-control "$IMMUTABLE_CACHE"
else
    echo "    none staged — the style's raster source will 404 at low zoom" >&2
fi

echo "==> vector tiles (${PMTILES_NAME}, multipart)"
s3 cp "${DIST_DIR}/${PMTILES_NAME}" "s3://${R2_BUCKET}/${PMTILES_NAME}" \
    --content-type application/octet-stream \
    --cache-control "$IMMUTABLE_CACHE"

echo "==> style (published last)"
s3 cp "${DIST_DIR}/styles/liberty" "s3://${R2_BUCKET}/styles/liberty" \
    --content-type application/json \
    --cache-control "$STYLE_CACHE"

echo "==> published to ${TILES_ORIGIN}"
echo "    verify with: ./scripts/verify.sh"
