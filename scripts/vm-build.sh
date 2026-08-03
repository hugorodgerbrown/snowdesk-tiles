#!/usr/bin/env bash
# Build the archive on a throwaway cloud VM and upload it straight to R2.
#
# Run this ON the VM, not on your laptop. The point is that the ~28 GB source,
# planetiler's working files and the finished archive never touch your machine —
# only R2 ever receives the output.
#
# Only the archive is built here. The style, sprites, glyphs and Natural Earth
# raster are already published and unaffected by a rebuild: the style names the
# tile URL template, not the archive, and the Worker resolves the archive through
# PMTILES_KEY. So this uploads one object and nothing else changes.
#
#     curl -fsSL https://raw.githubusercontent.com/hugorodgerbrown/snowdesk-tiles/main/scripts/vm-build.sh | bash
#
# or, having cloned the repo:
#
#     CLOUDFLARE_ACCOUNT_ID=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
#         ./scripts/vm-build.sh
#
# Use an R2 token scoped to Object Read & Write. It lives in this VM's shell
# history and environment, so destroy the VM afterwards rather than keeping it
# around — that is the cheapest form of credential rotation.

set -euo pipefail

if [ -f scripts/config.sh ]; then
    cd "$(dirname "$0")/.."
else
    echo "==> cloning snowdesk-tiles"
    git clone --depth 1 https://github.com/hugorodgerbrown/snowdesk-tiles.git
    cd snowdesk-tiles
fi
# shellcheck source=scripts/config.sh
source scripts/config.sh

: "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID}"
: "${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID from an R2 token}"
: "${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY from an R2 token}"

echo "==> installing build dependencies"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq openjdk-21-jre-headless awscli
else
    echo "warning: not a Debian/Ubuntu host — install Java 21 and awscli yourself" >&2
fi

free_gb=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
if [ "${free_gb:-0}" -lt 100 ]; then
    echo "error: ${free_gb} GB free, need ~100 GB (28 GB source + working files + output)" >&2
    exit 1
fi

echo "==> building ${PMTILES_NAME} for ${PLANETILER_BOUNDS}"
# Give planetiler most of the box's RAM; it is the whole reason for renting one.
total_mb=$(free -m | awk '/^Mem:/ {print $2}')
export PLANETILER_MEMORY="$(( total_mb * 3 / 4 ))m"
./scripts/build-extract.sh

echo "==> uploading to R2"
aws s3 cp "${DIST_DIR}/${PMTILES_NAME}" "s3://${R2_BUCKET}/${PMTILES_NAME}" \
    --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" \
    --region auto \
    --content-type application/octet-stream \
    --cache-control "$IMMUTABLE_CACHE" \
    --no-progress

cat <<EOF

==> done. ${PMTILES_NAME} is live in R2.

Back on your laptop:

    ./scripts/verify.sh

Then destroy this VM — the R2 credentials are in its environment.
EOF
