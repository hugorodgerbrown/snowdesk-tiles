#!/usr/bin/env bash
# Cut the 3-D map's elevation copy from Mapterhorn's archives (SNOW-1444).
#
#     ./scripts/build-dem.sh 2026-11
#
# Writes dist/dem/<version>/: planet.pmtiles (z0-12), one 6-<x>-<y>.pmtiles per
# Mapterhorn regional archive the area touches (z13 to DEM_MAX_ZOOM), and
# manifest.json naming the Mapterhorn release and the area they were cut from.
# Each is clipped to DEM_AREA with `pmtiles extract --region`, which reads only
# the byte ranges it needs from download.mapterhorn.com — the planet archive is
# 355 GB and the Alpine regional ones hundreds more, none of it downloaded whole.
#
# The version is required, and upload.sh refuses one that is already published:
# production reads a version by name (MAP_3D_DEM_TILE_URL), so replacing the one
# it reads would change the map under it with no way back. Use the month,
# 2026-11.
#
# Every archive resumes: one already in dist/ is kept. FORCE_DEM=1 redoes them.
#
# Requires: the pmtiles CLI (github.com/protomaps/go-pmtiles), python3, curl.
# vm-build.sh dem installs them and runs this on a throwaway box.

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

version=${1:-}
if ! [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    echo "usage: $0 <version>   (a plain token such as 2026-11)" >&2
    exit 2
fi
command -v pmtiles >/dev/null 2>&1 || {
    echo "error: pmtiles CLI not found — https://github.com/protomaps/go-pmtiles/releases" >&2
    exit 1
}
[ -s "$DEM_AREA" ] || {
    echo "error: no area at ${DEM_AREA} — run export_supported_area in snowdesk-data-pipeline" >&2
    exit 1
}

out="${DIST_DIR}/${DEM_DIR}/${version}"
mkdir -p "$out"

echo "==> Mapterhorn catalogue"
catalogue="${out}/download_urls.json"
curl -fsS "${DEM_SOURCE_ORIGIN}/download_urls.json" -o "$catalogue"
echo "    release $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version"))' "$catalogue")"

extract() {
    local name=$1 max_zoom=$2
    local target="${out}/${name}"
    if [ -s "$target" ] && [ "${FORCE_DEM:-0}" != "1" ]; then
        echo "    ${name}: already built — keeping it (FORCE_DEM=1 to redo)"
        return
    fi
    echo "    ${name}: extracting to z${max_zoom}"
    # Into a .part file and renamed after, so an interrupted extract is never
    # mistaken for a finished one on the resume above.
    pmtiles extract "${DEM_SOURCE_ORIGIN}/${name}" "${target}.part" \
        --region="$DEM_AREA" \
        --maxzoom="$max_zoom" \
        --download-threads="$DEM_DOWNLOAD_THREADS"
    mv "${target}.part" "$target"
}

echo "==> planet.pmtiles, z0-12"
extract planet.pmtiles 12

mapfile -t regional < <(python3 scripts/dem_archives.py select "$DEM_AREA" "$catalogue")
echo "==> ${#regional[@]} regional archive(s), z13-${DEM_MAX_ZOOM}"
for name in "${regional[@]}"; do
    extract "$name" "$DEM_MAX_ZOOM"
done

python3 scripts/dem_archives.py manifest "$version" "$DEM_AREA" "$catalogue" \
    "${regional[@]}" --max-zoom "$DEM_MAX_ZOOM" >"${out}/manifest.json"
rm -f "$catalogue"

echo "==> built ${out} ($(du -sh "$out" | cut -f1))"
echo "    publish with: op run --env-file=.env.1password -- ./scripts/upload.sh"
echo "    then: DEM_VERSION=${version} ./scripts/verify.sh"
