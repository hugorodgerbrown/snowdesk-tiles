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
        published=$((published + 1))
        return 0
    fi
    echo "    none staged — leaving the published ${1} as they are"
    return 1
}

# Nothing staged at all is a mistake — a clone with no build run, or DIST_DIR
# pointing at the wrong tree — and it would otherwise exit 0 having published
# nothing. Counted rather than checked up front because which artefacts are
# staged is exactly what varies between a style-only publish, a terrain publish
# from the build box, and a full one.
published=0

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
    # Two passes because --content-type applies to the whole sync. The PNGs
    # stay published beside their WebP twins: the style that names them has a
    # one-hour TTL, and offline areas pinned before the switch hold PNG URLs.
    s3 sync "${DIST_DIR}/natural_earth" "s3://${R2_BUCKET}/natural_earth" \
        --exclude '*' --include '*.png' \
        --content-type image/png \
        --cache-control "$IMMUTABLE_CACHE"
    s3 sync "${DIST_DIR}/natural_earth" "s3://${R2_BUCKET}/natural_earth" \
        --exclude '*' --include '*.webp' \
        --content-type image/webp \
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
    published=$((published + 1))
    s3 cp "${DIST_DIR}/${PMTILES_NAME}" "s3://${R2_BUCKET}/${PMTILES_NAME}" \
        --content-type application/octet-stream \
        --cache-control "$IMMUTABLE_CACHE"
fi

echo "==> terrain elevation tiles"
# Tens of thousands of small objects rather than one large one, so `sync` is
# doing real work here: a rerun after an interrupted upload transfers only what
# is missing. Class A operations are billed per thousand writes, which at this
# count is cents, and only on the first publish.
if mirrored "$TERRAIN_DIR"; then
    # grid.json is excluded and copied below. It is JSON, not an Int16 tile, and
    # it is the pointer to everything else here — so it needs a different
    # Content-Type, a short TTL, and to land last.
    s3 sync "${DIST_DIR}/${TERRAIN_DIR}" "s3://${R2_BUCKET}/${TERRAIN_DIR}" \
        --exclude 'grid.json' \
        --content-type application/octet-stream \
        --cache-control "$IMMUTABLE_CACHE"

    if [ -f "${DIST_DIR}/${TERRAIN_DIR}/grid.json" ]; then
        echo "    grid definition (published last, after its tiles)"
        s3 cp "${DIST_DIR}/${TERRAIN_DIR}/grid.json" \
            "s3://${R2_BUCKET}/${TERRAIN_DIR}/grid.json" \
            --content-type application/json \
            --cache-control "$STYLE_CACHE"
    else
        echo "    warning: no grid.json — SNOW-917 reads the grid definition" >&2
        echo "             from it, so the tileset is unusable without one" >&2
    fi
fi

echo "==> terrain-class tiles"
# Same shape as the elevation tiles: tens of thousands of small immutable
# objects synced, and the descriptor copied last because it is the pointer the
# browser reads the contract and the URL template from.
#
# Unlike the elevation tiles this sync passes --delete. The bucket keys carry no
# version and the Worker strips it, so a tile a rebuild no longer produces would
# otherwise go on being served as current classes rather than a 204. That makes
# a partial staging dangerous — it would delete every level it lacks — so a
# directory without tiles.json, which cut_terrain_class_tiles.py writes only for
# a build of every zoom, is refused rather than published.
if mirrored "$TERRAIN_CLASS_DIR"; then
    if [ ! -f "${DIST_DIR}/${TERRAIN_CLASS_DIR}/tiles.json" ]; then
        echo "error: ${DIST_DIR}/${TERRAIN_CLASS_DIR} has no tiles.json — a partial" >&2
        echo "       build, or one that did not finish; not publishing it" >&2
        exit 1
    fi
    # --exclude also keeps the live tiles.json out of the deletion.
    s3 sync "${DIST_DIR}/${TERRAIN_CLASS_DIR}" "s3://${R2_BUCKET}/${TERRAIN_CLASS_DIR}" \
        --delete \
        --exclude 'tiles.json' \
        --content-type image/png \
        --cache-control "$IMMUTABLE_CACHE"

    echo "    descriptor (published last, after its tiles)"
    s3 cp "${DIST_DIR}/${TERRAIN_CLASS_DIR}/tiles.json" \
        "s3://${R2_BUCKET}/${TERRAIN_CLASS_DIR}/tiles.json" \
        --content-type application/json \
        --cache-control "$STYLE_CACHE"
fi

echo "==> 3-D elevation copy"
# One directory per version, each a handful of multi-GB archives plus
# manifest.json. Versions sit side by side in the bucket and the Worker reads
# the one it is asked for, so this never deletes anything: retiring an old
# version is a deliberate `aws s3 rm --recursive s3://<bucket>/dem/<version>/`,
# once production no longer reads it.
#
# A version already in the bucket is skipped, never replaced. Production reads a
# version by name, and its tiles are cached immutable for a year, so replacing
# one in place would mix two builds under one URL with no way back. Skipping
# rather than failing lets a box that published a copy go on to publish other
# things. An upload interrupted before manifest.json (the last object) landed is
# not yet published, so a rerun simply resumes it. FORCE_DEM_PUBLISH=1 replaces
# a published version, and is only safe for one no client has ever read.
if mirrored "$DEM_DIR"; then
    for dir in "${DIST_DIR}/${DEM_DIR}"/*/; do
        version=$(basename "$dir")
        if [ ! -f "${dir}manifest.json" ] || [ ! -f "${dir}planet.pmtiles" ]; then
            echo "error: ${dir} has no manifest.json or planet.pmtiles — an unfinished" >&2
            echo "       build-dem.sh; not publishing it" >&2
            exit 1
        fi
        if aws s3api head-object --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET" \
            --key "${DEM_DIR}/${version}/manifest.json" >/dev/null 2>&1 \
            && [ "${FORCE_DEM_PUBLISH:-0}" != "1" ]; then
            echo "    ${version}: already published — skipping; a refresh is a new version"
            continue
        fi
        echo "    ${version}"
        s3 sync "$dir" "s3://${R2_BUCKET}/${DEM_DIR}/${version}" \
            --exclude '*' --include '*.pmtiles' \
            --content-type application/octet-stream \
            --cache-control "$IMMUTABLE_CACHE"
        # Last: it is what marks the version as published, above and in
        # verify.sh.
        s3 cp "${dir}manifest.json" "s3://${R2_BUCKET}/${DEM_DIR}/${version}/manifest.json" \
            --content-type application/json \
            --cache-control "$IMMUTABLE_CACHE"
    done
fi

echo "==> style (published last)"
# Same rule as the mirror directories, for the same reason: absent means "not
# rebuilt", never "delete it". build-terrain.sh stages only dist/terrain/ (and
# build-terrain-class.sh only dist/terrain-class/), so a
# box that built the tileset has no style to publish, and requiring one would
# mean mirroring ~1 GB of unchanged assets to get it.
if [ -f "${DIST_DIR}/styles/liberty" ]; then
    published=$((published + 1))
    s3 cp "${DIST_DIR}/styles/liberty" "s3://${R2_BUCKET}/styles/liberty" \
        --content-type application/json \
        --cache-control "$STYLE_CACHE"
else
    echo "    none staged — leaving the published style as it is"
fi

if [ "$published" -eq 0 ]; then
    cat >&2 <<EOF
error: nothing staged in ${DIST_DIR}/ — published nothing

Run ./scripts/build.sh for the style and the mirrored assets,
./scripts/build-terrain.sh for the elevation tileset,
./scripts/build-terrain-class.sh for the class tiles, or
./scripts/build-dem.sh <version> for the 3-D elevation copy, then rerun this.
EOF
    exit 1
fi

echo "==> published to ${TILES_ORIGIN}"
echo "    verify with: ./scripts/verify.sh"
