#!/usr/bin/env bash
# Build one of the two big artefacts on a throwaway cloud VM and upload it
# straight to R2.
#
#     ./snowdesk-tiles/scripts/vm-build.sh            # the vector tile archive
#     ./snowdesk-tiles/scripts/vm-build.sh terrain    # the elevation tileset
#     ./snowdesk-tiles/scripts/vm-build.sh terrain-class   # the class tiles
#     ./snowdesk-tiles/scripts/vm-build.sh dem 2026-11     # the 3-D elevation copy
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
# Credentials, in order of preference:
#
#   1. Already in the environment (CI, say): used as they are.
#   2. A 1Password service account token. It asks for one first; paste it and
#      the script installs the 1Password CLI and resolves the R2 values from
#      .env.1password itself (scripts/op-env.sh), so one secret crosses to the
#      box instead of three. A service account cannot read a Private vault, so
#      the item must sit in a vault the account is granted; point OP_ENV_FILE
#      at a references file for that vault if it is not the one in
#      .env.1password.
#   3. Press Enter at that prompt and it asks for the three R2 values instead.
#
# All prompts read with -s rather than taking values on the command line, so
# they stay out of shell history and the process table.
#
# Use an R2 token scoped to Object Read & Write, and destroy the VM when the
# build is done — the credentials are in its memory, and deleting the box is the
# cheapest rotation there is.

set -euo pipefail

target=${1:-basemap}
case $target in
    basemap | terrain | terrain-class) ;;
    dem)
        # The version names the copy in its URL; build-dem.sh insists on one.
        dem_version=${2:-}
        [ -n "$dem_version" ] || { echo "usage: $0 dem <version>   e.g. 2026-11" >&2; exit 2; }
        ;;
    *)
        echo "usage: $0 [basemap|terrain|terrain-class|dem <version>]" >&2
        exit 2
        ;;
esac

# Overridable so a VM can build from a branch when iterating on the pipeline.
: "${REPO_URL:=https://github.com/hugorodgerbrown/snowdesk-tiles.git}"
: "${REPO_BRANCH:=main}"

# Look for config.sh beside this script, not in the working directory: the
# README runs it as ./snowdesk-tiles/scripts/vm-build.sh from $HOME, where a
# cwd-relative check missed the checkout and tried to clone over it. When the
# script is piped to bash there is no checkout beside it, and it clones.
script_dir=$(cd "$(dirname "$0")" && pwd)
if [ -f "${script_dir}/config.sh" ]; then
    cd "${script_dir}/.."
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

# The 1Password CLI, from 1Password's own apt repository, verified with their
# signing key — the steps in their Linux install guide.
install_op_cli() {
    if command -v op >/dev/null 2>&1; then return; fi
    command -v apt-get >/dev/null 2>&1 || {
        echo "error: install the 1Password CLI (op) yourself on a non-Debian host" >&2
        exit 1
    }
    echo "==> installing the 1Password CLI"
    local arch key=/usr/share/keyrings/1password-archive-keyring.gpg
    local policy=AC2D62742012EA22
    arch=$(dpkg --print-architecture)
    sudo apt-get update -qq
    sudo apt-get install -y -qq curl gpg
    curl -fsS https://downloads.1password.com/linux/keys/1password.asc \
        | sudo gpg --batch --yes --dearmor --output "$key"
    echo "deb [arch=${arch} signed-by=${key}] https://downloads.1password.com/linux/debian/${arch} stable main" \
        | sudo tee /etc/apt/sources.list.d/1password.list >/dev/null
    sudo mkdir -p "/etc/debsig/policies/${policy}" "/usr/share/debsig/keyrings/${policy}"
    curl -fsS https://downloads.1password.com/linux/debian/debsig/1password.pol \
        | sudo tee "/etc/debsig/policies/${policy}/1password.pol" >/dev/null
    curl -fsS https://downloads.1password.com/linux/keys/1password.asc \
        | sudo gpg --batch --yes --dearmor --output "/usr/share/debsig/keyrings/${policy}/debsig.gpg"
    sudo apt-get update -qq
    sudo apt-get install -y -qq 1password-cli
}

r2_names=(CLOUDFLARE_ACCOUNT_ID AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY)
r2_missing=0
for name in "${r2_names[@]}"; do [ -n "${!name:-}" ] || r2_missing=1; done
if [ "$r2_missing" = 1 ] && [ -z "${SNOWDESK_OP_RUN:-}" ] && [ "${USE_1PASSWORD:-1}" != "0" ]; then
    if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
        printf '1Password service account token (Enter to type the R2 values instead): ' >&2
        read -rs OP_SERVICE_ACCOUNT_TOKEN
        printf '\n' >&2
    fi
    if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
        export OP_SERVICE_ACCOUNT_TOKEN
        install_op_cli
        # shellcheck source=scripts/op-env.sh
        source scripts/op-env.sh
        # Re-runs this script under op run, so the build and upload.sh below
        # see the values; it does not return.
        op_reexec "${PWD}/scripts/vm-build.sh" "${r2_names[@]}" -- "$@"
    fi
fi

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

# The pmtiles CLI, for the elevation copy (SNOW-1444). Not in apt; a single Go
# binary from protomaps' releases. PMTILES_CLI_VERSION pins one; unset, the
# latest release is used, read from the redirect GitHub gives /releases/latest.
install_pmtiles_cli() {
    if command -v pmtiles >/dev/null 2>&1; then
        echo "    pmtiles already installed ($(pmtiles version 2>&1 | head -1))"
        return
    fi
    local version=${PMTILES_CLI_VERSION:-} arch tmp
    if [ -z "$version" ]; then
        version=$(curl -fsSI https://github.com/protomaps/go-pmtiles/releases/latest \
            | tr -d '\r' | sed -n 's|^location: .*/tag/v||Ip')
    fi
    [ -n "$version" ] || { echo "error: could not resolve a pmtiles release" >&2; exit 1; }
    case $(uname -m) in
        x86_64) arch=x86_64 ;;
        aarch64 | arm64) arch=arm64 ;;
        *) echo "error: no pmtiles build for $(uname -m)" >&2; exit 1 ;;
    esac
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/protomaps/go-pmtiles/releases/download/v${version}/go-pmtiles_${version}_Linux_${arch}.tar.gz" \
        -o "${tmp}/pmtiles.tar.gz"
    tar -xzf "${tmp}/pmtiles.tar.gz" -C "$tmp" pmtiles
    sudo install -m 0755 "${tmp}/pmtiles" /usr/local/bin/pmtiles
    rm -rf "$tmp"
    echo "    pmtiles ${version} installed"
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
        dem) sudo apt-get install -y -qq python3 curl unzip ;;
    esac
    install_aws_cli
    [ "$target" = "dem" ] && install_pmtiles_cli
else
    echo "warning: not a Debian/Ubuntu host — install the dependencies yourself" >&2
    echo "         basemap: Java 21 + awscli v2; terrain: GDAL + Python 3.12 + awscli v2" >&2
    echo "         terrain-class: as terrain, plus numpy"
    echo "         dem: Python 3.12 + the pmtiles CLI + awscli v2" >&2
fi

# The first two want ~100 GB, for different reasons: 28 GB of OSM source plus
# planetiler's working files, or 44 GB of GeoTIFFs plus a 7 GB warped raster, a
# 7.1 GB flat grid and 3.6 GB of tiles. terrain-class wants ~25 GB more on top
# of the terrain build it starts from.
need_gb=100
[ "$target" = "terrain-class" ] && need_gb=125
# The elevation copy is an estimate until the first build reports it: z13-15 is
# about 1.5% of a z18 regional archive's bytes and 6% of a z17 one's, which over
# the seven Alpine and Pyrenean archives comes to roughly 20-40 GB.
[ "$target" = "dem" ] && need_gb=80
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
elif [ "$target" = "dem" ]; then
    echo "==> building the 3-D elevation copy ${dem_version}"
    ./scripts/build-dem.sh "$dem_version"

    # upload.sh publishes only what is staged — here dist/dem/ — and never
    # replaces a version that is already in the bucket.
    echo "==> uploading to R2"
    ./scripts/upload.sh

    artefact="${DEM_DIR}/${dem_version}/ ($(du -sh "${DIST_DIR}/${DEM_DIR}/${dem_version}" | cut -f1))"
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
    fi
    # Unconditional: an earlier `terrain` run on this same box may have left
    # its tiles staged, and upload.sh publishes whatever dist/ holds.
    rm -rf "${DIST_DIR:?}/${TERRAIN_DIR:?}"

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

    ./scripts/verify.sh          # DEM_VERSION=<version> for the elevation copy

Then destroy this VM — the R2 credentials are in its environment.
EOF
