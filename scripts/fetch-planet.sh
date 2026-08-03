#!/usr/bin/env bash
# Fetch OpenFreeMap's own planet build and convert it to PMTiles (SNOW-485).
#
# This is the route to coverage identical to tiles.openfreemap.org, rather than
# merely similar: it is their file, built by their planetiler run, in the same
# OpenMapTiles schema Liberty expects. Nothing is re-derived, so nothing can be
# clipped differently.
#
# Use this instead of build-extract.sh. A Geofabrik regional extract is clipped
# to a polygon, not a bounding box, and the "alps" polygon leaves holes in
# Switzerland — Basel returns an empty tile, and the Jura is effectively absent.
#
# OpenFreeMap publishes Btrfs and MBTiles, not PMTiles, so a conversion step is
# needed. Their reason for avoiding PMTiles is worth knowing and does not apply
# here: they cite "range requests in 90 GB files have terrible latency on
# Cloudflare". That is the client-side pmtiles:// pattern. Our Worker reads the
# archive through an R2 binding, not HTTP, and serves discrete XYZ tiles that
# edge-cache normally.
#
# Requires: curl, go-pmtiles (`brew install pmtiles`), and a lot of disk —
# roughly 210 GB free to hold the download and the conversion output together.
#
#     ./scripts/fetch-planet.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

: "${OFM_BTRFS_HOST:=https://btrfs.openfreemap.com}"
: "${OFM_PLANET_VERSION:=}"
: "${WORK_DIR:=${DIST_DIR}/planet-work}"
# Download + converted output, with headroom. Both exist at once.
: "${REQUIRED_GB:=210}"

if ! command -v pmtiles >/dev/null 2>&1; then
    echo "error: go-pmtiles not found — brew install pmtiles" >&2
    exit 1
fi

mkdir -p "$WORK_DIR"

available_gb=$(df -g "$WORK_DIR" | awk 'NR==2 {print $4}')
if [ "${available_gb:-0}" -lt "$REQUIRED_GB" ]; then
    echo "error: ${available_gb} GB free at ${WORK_DIR}, need ~${REQUIRED_GB} GB" >&2
    echo "       (a ~95 GB download plus a similarly sized conversion output)" >&2
    exit 1
fi

# Resolve the newest planet build unless one was named. Versions are dated
# directories; the index lists them oldest first.
if [ -z "$OFM_PLANET_VERSION" ]; then
    echo "==> resolving latest planet build"
    OFM_PLANET_VERSION=$(curl -fsS "${OFM_BTRFS_HOST}/files.txt" \
        | grep -E '^areas/planet/[0-9]+_[0-9]+_pt/tiles\.mbtiles$' \
        | tail -1 \
        | cut -d/ -f3)
fi
if [ -z "$OFM_PLANET_VERSION" ]; then
    echo "error: could not resolve a planet version from ${OFM_BTRFS_HOST}/files.txt" >&2
    exit 1
fi

url="${OFM_BTRFS_HOST}/areas/planet/${OFM_PLANET_VERSION}/tiles.mbtiles"
mbtiles="${WORK_DIR}/${OFM_PLANET_VERSION}.mbtiles"
output="${DIST_DIR}/${PMTILES_NAME}"

echo "==> version ${OFM_PLANET_VERSION}"
echo "    ${url}"

# -C - resumes a partial download rather than restarting ~95 GB.
echo "==> downloading (resumable; safe to interrupt and rerun)"
curl -fL -C - --progress-bar -o "$mbtiles" "$url"

echo "==> converting to PMTiles"
pmtiles convert "$mbtiles" "$output"

echo "==> ${output} is $(du -h "$output" | cut -f1)"
echo "    the .mbtiles is no longer needed: rm -rf ${WORK_DIR}"
echo
echo "Next: set PMTILES_NAME (scripts/config.sh) and PMTILES_KEY"
echo "(worker/wrangler.toml) to ${PMTILES_NAME}, then build.sh and upload.sh."
