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
#     git clone -b BRANCH https://github.com/hugorodgerbrown/snowdesk-tiles.git
#     ./snowdesk-tiles/scripts/vm-build.sh
#
# Not `curl ... | bash`: the prompts below read from stdin, which that form has
# already given to the script itself.
#
# It prompts for the three R2 values rather than taking them on the command line,
# so they stay out of shell history and the process table. Set them in the
# environment beforehand if you would rather (CI, say).
#
# Use an R2 token scoped to Object Read & Write, and destroy the VM when the
# build is done — the credentials are in its memory, and deleting the box is the
# cheapest rotation there is.

set -euo pipefail

# REPO_BRANCH matters until the branch is merged: main does not have this script
# yet, so a default clone would fetch a tree without it.
: "${REPO_URL:=https://github.com/hugorodgerbrown/snowdesk-tiles.git}"
: "${REPO_BRANCH:=main}"

if [ -f scripts/config.sh ]; then
    cd "$(dirname "$0")/.."
else
    echo "==> cloning snowdesk-tiles (${REPO_BRANCH})"
    command -v git >/dev/null 2>&1 || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" snowdesk-tiles
    cd snowdesk-tiles
fi
# shellcheck source=scripts/config.sh
source scripts/config.sh

# Prompt rather than take these on the command line: an inline assignment lands
# in shell history and is visible in the process table to anything else on the
# box. read -s keeps the secret off both.
prompt_secret() {
    local name=$1 prompt=$2 value
    if [ -n "${!name:-}" ]; then return; fi
    printf '%s: ' "$prompt" >&2
    read -rs value
    printf '\n' >&2
    [ -n "$value" ] || { echo "error: ${name} is required" >&2; exit 1; }
    export "$name=$value"
}

prompt_secret CLOUDFLARE_ACCOUNT_ID "Cloudflare account ID"
prompt_secret AWS_ACCESS_KEY_ID     "R2 access key ID"
prompt_secret AWS_SECRET_ACCESS_KEY "R2 secret access key"

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
