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

if [ ! -f "${DIST_DIR}/styles/liberty" ]; then
    echo "error: ${DIST_DIR}/styles/liberty missing — run build.sh first" >&2
    exit 1
fi

# The archive may legitimately not be here: vm-build.sh builds it on a rented
# box and uploads it from there, so the local tree never holds it. Requiring it
# would block the very workflow the runbook recommends. Only insist that it
# exists *somewhere* — locally to upload, or already in the bucket.
local_size=""
if [ -f "${DIST_DIR}/${PMTILES_NAME}" ]; then
    local_size=$(wc -c <"${DIST_DIR}/${PMTILES_NAME}" | tr -d ' ')
fi

remote_size=$(aws s3api head-object \
    --endpoint-url "$R2_ENDPOINT" \
    --bucket "$R2_BUCKET" \
    --key "$PMTILES_NAME" \
    --query ContentLength --output text 2>/dev/null || echo "")

if [ -z "$local_size" ] && [ -z "$remote_size" ]; then
    cat >&2 <<EOF
error: ${PMTILES_NAME} is neither in ${DIST_DIR}/ nor in the bucket

Build it first — ./scripts/build-extract.sh locally, or ./scripts/vm-build.sh
on a VM with the disk for it.
EOF
    exit 1
fi

# Order matters. The style names the PMTiles archive, so everything it points
# at must already be live before the style itself is published; otherwise a
# client that fetches the new style mid-upload gets a 404 on the tiles.

# A style-only publish is a real workflow — the style changes whenever the
# origin, the tile path or the zoom range moves, none of which touch the mirror.
# `aws s3 sync` errors on a missing local source, so an unstaged directory would
# abort the run rather than skip it, and the fix would be re-downloading ~1 GB of
# assets that are already published and unchanged. Absent means "not rebuilt",
# never "delete it" — nothing here syncs with `--delete`.
mirrored() {
    if [ -d "${DIST_DIR}/$1" ]; then
        return 0
    fi
    echo "    none staged — leaving the published ${1} as they are"
    return 1
}

echo "==> glyphs"
if mirrored fonts; then
    s3 sync "${DIST_DIR}/fonts" "s3://${R2_BUCKET}/fonts" \
        --content-type application/x-protobuf \
        --cache-control "$IMMUTABLE_CACHE"
fi

echo "==> sprite atlases and indexes"
if mirrored sprites; then
    s3 sync "${DIST_DIR}/sprites" "s3://${R2_BUCKET}/sprites" \
        --exclude '*' --include '*.png' \
        --content-type image/png \
        --cache-control "$IMMUTABLE_CACHE"
    s3 sync "${DIST_DIR}/sprites" "s3://${R2_BUCKET}/sprites" \
        --exclude '*' --include '*.json' \
        --content-type application/json \
        --cache-control "$IMMUTABLE_CACHE"
fi

echo "==> natural earth raster tiles"
if mirrored natural_earth; then
    s3 sync "${DIST_DIR}/natural_earth" "s3://${R2_BUCKET}/natural_earth" \
        --content-type image/png \
        --cache-control "$IMMUTABLE_CACHE"
fi

echo "==> vector tiles (${PMTILES_NAME}, multipart)"
# `cp` always re-uploads, and the archive is several GB. It changes only when
# the extract is rebuilt, whereas the style changes whenever the origin or tile
# path moves — so a style-only publish would otherwise push the whole archive
# again for nothing. Sizes were compared above; FORCE_ARCHIVE=1 overrides (a
# rebuild landing on an identical byte count is possible, if unlikely).
if [ -z "$local_size" ]; then
    echo "    not built locally, already in the bucket (${remote_size} bytes) — skipping"
elif [ "${FORCE_ARCHIVE:-0}" != "1" ] && [ "$local_size" = "$remote_size" ]; then
    echo "    unchanged (${local_size} bytes) — skipping; FORCE_ARCHIVE=1 to override"
else
    s3 cp "${DIST_DIR}/${PMTILES_NAME}" "s3://${R2_BUCKET}/${PMTILES_NAME}" \
        --content-type application/octet-stream \
        --cache-control "$IMMUTABLE_CACHE"
fi

echo "==> style (published last)"
s3 cp "${DIST_DIR}/styles/liberty" "s3://${R2_BUCKET}/styles/liberty" \
    --content-type application/json \
    --cache-control "$STYLE_CACHE"

echo "==> published to ${TILES_ORIGIN}"
echo "    verify with: ./scripts/verify.sh"
