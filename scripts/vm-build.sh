#!/usr/bin/env bash
# Build one of the two big artefacts on a throwaway cloud VM and upload it
# straight to R2.
#
#     ./snowdesk-tiles/scripts/vm-build.sh            # the vector tile archive
#     ./snowdesk-tiles/scripts/vm-build.sh terrain    # the elevation tileset
#     ./snowdesk-tiles/scripts/vm-build.sh terrain-class   # the class tiles
#
# Run this ON the VM, not on your laptop. The point is that the tens of GB of
# source, the working files and the finished output never touch your machine —
# only R2 ever receives the result.
#
# Either target publishes one artefact and leaves everything else in the bucket
# alone. The basemap uploads a single object: the style names the tile URL
# template rather than the archive, and the Worker resolves the archive through
# PMTILES_KEY, so nothing else changes. Terrain uploads its tiles and grid.json
# through upload.sh, which publishes only what is staged in dist/. terrain-class
# is derived from the elevation grid, so on a fresh box it builds that first —
# as an input only; see below.
#
#     git clone https://github.com/hugorodgerbrown/snowdesk-tiles.git
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

target=${1:-basemap}
case $target in
    basemap | terrain | terrain-class) ;;
    *)
        echo "usage: $0 [basemap|terrain|terrain-class]" >&2
        exit 2
        ;;
esac

# Overridable so a VM can build from a branch when iterating on the pipeline.
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

# The AWS CLI does not come from apt. Ubuntu 24.04 — the image the runbook
# tells you to pick — has no `awscli` candidate at all, and the v1 that older
# releases carry is not what upload.sh is written against. So take v2 from AWS
# directly, and skip it when a real one is already on the box.
install_aws_cli() {
    if command -v aws >/dev/null 2>&1; then
        echo "    aws already installed ($(aws --version 2>&1))"
        return
    fi
    local tmp
    tmp=$(mktemp -d)
    curl -fsS "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" \
        -o "${tmp}/awscliv2.zip"
    unzip -q "${tmp}/awscliv2.zip" -d "$tmp"
    sudo "${tmp}/aws/install" --update
    rm -rf "$tmp"
}

echo "==> installing build dependencies for ${target}"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    # planetiler is a JVM; the terrain pipeline is GDAL and stdlib Python.
    case $target in
        basemap) sudo apt-get install -y -qq openjdk-21-jre-headless curl unzip ;;
        terrain) sudo apt-get install -y -qq gdal-bin python3 curl unzip ;;
        # numpy for the classifier, the one script in the repo that needs it.
        terrain-class) sudo apt-get install -y -qq gdal-bin python3 python3-numpy curl unzip ;;
    esac
    install_aws_cli
else
    echo "warning: not a Debian/Ubuntu host — install the dependencies yourself" >&2
    echo "         basemap: Java 21 + awscli v2; terrain: GDAL + Python 3.12 + awscli v2" >&2
    echo "         terrain-class: as terrain, plus numpy" >&2
fi

# The first two want ~100 GB, for different reasons: 28 GB of OSM source plus
# planetiler's working files, or 44 GB of GeoTIFFs plus a 7 GB warped raster, a
# 7.1 GB flat grid and 3.6 GB of tiles. terrain-class wants ~25 GB more on top
# of the terrain build it starts from.
need_gb=100
[ "$target" = "terrain-class" ] && need_gb=125
free_gb=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
if [ "${free_gb:-0}" -lt "$need_gb" ]; then
    echo "error: ${free_gb} GB free, need ~${need_gb} GB for the ${target} build" >&2
    exit 1
fi

if [ "$target" = "basemap" ]; then
    echo "==> building ${PMTILES_NAME} for ${PLANETILER_BOUNDS}"
    # Give planetiler most of the box's RAM; it is the whole reason for renting
    # one.
    total_mb=$(free -m | awk '/^Mem:/ {print $2}')
    export PLANETILER_MEMORY="$(( total_mb * 3 / 4 ))m"
    ./scripts/build-extract.sh

    # Not upload.sh: that skips an archive whose byte count matches the bucket,
    # and a rebuild landing on an identical size is possible. One object, one
    # unconditional copy.
    echo "==> uploading to R2"
    aws s3 cp "${DIST_DIR}/${PMTILES_NAME}" "s3://${R2_BUCKET}/${PMTILES_NAME}" \
        --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" \
        --region auto \
        --content-type application/octet-stream \
        --cache-control "$IMMUTABLE_CACHE" \
        --no-progress

    artefact="${PMTILES_NAME}"
elif [ "$target" = "terrain" ]; then
    echo "==> building the terrain tileset for ${TERRAIN_BBOX}"
    ./scripts/build-terrain.sh

    # upload.sh here, not a bespoke copy: this is tens of thousands of small
    # objects, so `sync` is doing real work — an interrupted upload resumes and
    # transfers only what is missing — and grid.json has to land last, with its
    # own Content-Type and TTL. The script publishes only what is staged, so the
    # style and the mirror are left as they are.
    echo "==> uploading to R2"
    ./scripts/upload.sh

    artefact="${TERRAIN_DIR}/ ($(find "${DIST_DIR}/${TERRAIN_DIR}" -name '*.s16' | wc -l | tr -d ' ') tiles)"
else
    # The classes are computed from grid.raw, which only build-terrain.sh makes.
    # On a fresh box that means the whole elevation build first — ~90 minutes,
    # most of it the download — and then its staged tiles are removed before
    # the upload. This target publishes the class tiles and nothing else: the
    # live elevation grid is what SNOW-917 samples, and replacing it with a
    # rebuild nobody asked for and nobody has verified is not a side effect a
    # class build should have. (If swisstopo have published new squares since
    # the live grid was built, the classes will describe the newer heights;
    # verify.sh compares them against the live grid and will say so.)
    if [ ! -s "${TERRAIN_WORK_DIR}/grid.raw" ]; then
        echo "==> no ${TERRAIN_WORK_DIR}/grid.raw — building the elevation grid first"
        ./scripts/build-terrain.sh
        rm -rf "${DIST_DIR:?}/${TERRAIN_DIR:?}"
    fi

    echo "==> building the terrain-class tileset"
    ./scripts/build-terrain-class.sh

    # The Worker has to know the /terrain-class/ route before these are of any
    # use — until it is deployed they answer 404, since the bucket keys carry
    # no version — but nothing breaks either way round, so the deploy and this
    # upload can happen in either order. verify.sh needs both.
    echo "==> uploading to R2"
    ./scripts/upload.sh

    artefact="${TERRAIN_CLASS_DIR}/ ($(find "${DIST_DIR}/${TERRAIN_CLASS_DIR}" -name '*.png' | wc -l | tr -d ' ') tiles)"
fi

cat <<EOF

==> done. ${artefact} is live in R2.

Back on your laptop:

    ./scripts/verify.sh

Then destroy this VM — the R2 credentials are in its environment.
EOF
