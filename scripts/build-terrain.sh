#!/usr/bin/env bash
# Build the terrain elevation tileset from swissALTI3D (SNOW-908).
#
# Fetch swissALTI3D -> reproject to EPSG:3035 -> resample to 5 m -> quantise to
# Int16 -> cut skirted tiles into dist/terrain/. Then ./scripts/upload.sh.
#
#     ./scripts/build-terrain.sh
#     TERRAIN_BBOX="7.70 45.98 7.80 46.05" ./scripts/build-terrain.sh   # smoke test
#
# This is offline, one-off and slow. Terrain does not move on human timescales,
# so unlike the basemap in this repo there is no schedule and no refresh
# cadence: it is run by hand, and the reason it is a committed script rather
# than a sequence someone performed once is that the two things which would
# ever trigger a re-run — swisstopo's six-yearly re-survey, and a change to our
# own grid parameters — are both years apart. A re-run should be a diff, not an
# archaeology exercise.
#
# The grid geometry is deliberately NOT configurable here. Cell size, tile size,
# skirt width, projection and the height encoding live in scripts/terrain_grid.py
# because they are a contract with the Django side (SNOW-917), not a tunable.
# What is configurable is which ground to cover and where to work.
#
# Requires GDAL (gdalbuildvrt, gdalwarp, gdal_translate) and Python 3.12+.
#
#     Debian/Ubuntu:  sudo apt-get install -y gdal-bin python3
#     macOS:          brew install gdal
#
# Sizing, for all of Switzerland: ~41,000 source GeoTIFFs at about 1 MB each
# (~42 GB), a ~7 GB warped intermediate, a 7.1 GB flat grid and ~3.4 GB of
# tiles. Budget 100 GB free and a few hours, most of it the download — a 66 km2
# box around Zermatt ran end to end in 22 seconds, and the download is the only
# stage that scales with the number of source squares rather than with area.
# Every stage is skipped if its output is already there, so an interrupted run
# resumes; FORCE_TERRAIN=1 redoes the GDAL stages.
#
# Same advice as the basemap: rent a box rather than clearing the disk locally
# (see the README). It needs disk and cores, not the 16 GB of RAM planetiler
# wants.

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

# PYTHON must be a path to an interpreter, not a command line — it is invoked
# quoted, so "uv run python" would be looked up as a single executable name.
python=${PYTHON:-python3}

if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    cat >&2 <<EOF
error: ${python} is not a usable Python 3.12+ interpreter

PYTHON must be a path to an interpreter, not a command line. With uv:

    PYTHON=\$(uv python find 3.12) ./scripts/build-terrain.sh
EOF
    exit 1
fi

for tool in gdalbuildvrt gdalwarp gdal_translate; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: ${tool} not found — install GDAL (apt install gdal-bin, brew install gdal)" >&2
        exit 1
    }
done

work=$TERRAIN_WORK_DIR
tif_dir="${work}/tif"
urls="${work}/urls.txt"
manifest="${work}/manifest.json"
vrt="${work}/source.vrt"
warped="${work}/warped.tif"
raw="${work}/grid.raw"
out="${DIST_DIR}/${TERRAIN_DIR}"

mkdir -p "$tif_dir" "$out"

# Not fatal — a single region needs a fraction of this, and the check cannot
# know which you are building. Wrong is better said early than at 90%.
free_gb=$(df -Pk "$work" | awk 'NR==2 {print int($4 / 1048576)}')
if [ "${free_gb:-0}" -lt 80 ]; then
    echo "warning: ${free_gb} GB free; all of Switzerland needs ~60 GB of" >&2
    echo "         intermediates and tiles, so budget 100 GB" >&2
fi

# --- 1. Which squares --------------------------------------------------------

if [ -s "$urls" ] && [ -s "$manifest" ]; then
    echo "==> ${urls} already listed ($(wc -l <"$urls" | tr -d ' ') squares)"
else
    echo "==> listing swissALTI3D squares over ${TERRAIN_BBOX}"
    # shellcheck disable=SC2086 - TERRAIN_BBOX is four numbers, deliberately split
    "$python" scripts/fetch_swissalti3d.py \
        --bbox $TERRAIN_BBOX \
        --gsd "$TERRAIN_GSD" \
        --urls "$urls" \
        --manifest "$manifest"
fi

# --- 2. Download -------------------------------------------------------------
#
# Tens of thousands of small GeoTIFFs. curl per file with xargs -P rather than
# anything cleverer, because the only property that matters is that a run
# interrupted at 80% resumes at 80%: a file is downloaded to .part and renamed,
# so a half-written file is never mistaken for a finished one.

echo "==> downloading $(wc -l <"$urls" | tr -d ' ') GeoTIFFs into ${tif_dir}"
export TERRAIN_TIF_DIR="$tif_dir"
fetch_one() {
    local url=$1 dest
    dest="${TERRAIN_TIF_DIR}/$(basename "$url")"
    [ -s "$dest" ] && return 0
    curl -fsS --retry 3 --retry-delay 2 -o "${dest}.part" "$url" || {
        echo "error: failed ${url}" >&2
        return 1
    }
    mv "${dest}.part" "$dest"
}
export -f fetch_one
# `|| true` so a handful of failures do not abort under `set -e` before the
# count below can say how many are actually missing, which is the number that
# tells you whether to rerun or to go and look at something.
xargs -P "$TERRAIN_JOBS" -I{} bash -c 'fetch_one "$@"' _ {} <"$urls" || true

have=$(find "$tif_dir" -name '*.tif' | wc -l | tr -d ' ')
want=$(wc -l <"$urls" | tr -d ' ')
if [ "$have" -ne "$want" ]; then
    echo "error: ${have} of ${want} GeoTIFFs downloaded — rerun to resume" >&2
    exit 1
fi

# --- 3. Reproject and resample -----------------------------------------------

if [ ! -s "$vrt" ] || [ "$urls" -nt "$vrt" ]; then
    echo "==> building VRT over ${have} squares"
    find "$tif_dir" -name '*.tif' | sort >"${work}/tif-list.txt"
    gdalbuildvrt -input_file_list "${work}/tif-list.txt" -overwrite "$vrt"
fi

# The extent comes from the manifest — the union of the squares actually
# selected, not the box that was asked for. Coverage stops at the national
# border, and snapping the requested box would warp a wide margin of nothing.
read -r te_w te_s te_e te_n tile_x tile_y tiles_x tiles_y \
    < <("$python" scripts/terrain_grid.py extent --manifest "$manifest")
cell=$("$python" scripts/terrain_grid.py gdal-args cell-size)
dstnodata=$("$python" scripts/terrain_grid.py gdal-args dstnodata)
read -r s_min s_max d_min d_max \
    < <("$python" scripts/terrain_grid.py gdal-args scale)

cols=$(( (te_e - te_w) / cell ))
rows=$(( (te_n - te_s) / cell ))
echo "==> grid ${cols}x${rows} cells, ${tiles_x}x${tiles_y} tiles from (${tile_x}, ${tile_y})"

if [ -s "$warped" ] && [ "${FORCE_TERRAIN:-0}" != "1" ]; then
    echo "==> ${warped} exists — skipping warp; FORCE_TERRAIN=1 to redo"
else
    echo "==> warping to EPSG:3035 at ${cell} m (this is the slow part)"
    # -r average is the resample, and it is the whole reason the source is 2 m:
    # a box filter down to 5 m has something to average. Bilinear or nearest
    # would carry 2 m noise straight into a 5 m cell.
    #
    # Float32 here, not Int16: the quantisation onto the stored scale happens in
    # one exact linear step below, after the averaging. Rounding before the
    # average would put the rounding error into every neighbourhood twice.
    #
    # -srcnodata is not given. swissALTI3D's COGs declare GDAL_NODATA = -9999,
    # gdalwarp honours a declared source nodata by default, and average skips
    # masked cells — so the national border stays a clean edge instead of being
    # averaged into a cliff.
    gdalwarp \
        -t_srs EPSG:3035 \
        -te "$te_w" "$te_s" "$te_e" "$te_n" \
        -tr "$cell" "$cell" \
        -r average \
        -ot Float32 \
        -dstnodata "$dstnodata" \
        -multi -wo NUM_THREADS=ALL_CPUS \
        -co TILED=YES -co COMPRESS=DEFLATE -co BIGTIFF=YES \
        -overwrite \
        "$vrt" "$warped"
fi

# --- 4. Quantise to the stored Int16 scale -----------------------------------

if [ -s "$raw" ] && [ "${FORCE_TERRAIN:-0}" != "1" ]; then
    echo "==> ${raw} exists — skipping quantise; FORCE_TERRAIN=1 to redo"
else
    echo "==> quantising [${s_min}, ${s_max}] m onto Int16 [${d_min}, ${d_max}]"
    # An exact linear map of metres onto the stored scale: -scale's endpoints
    # are emitted by terrain_grid.py so the encoding here cannot drift from the
    # encoding SNOW-917 decodes with. The float nodata sentinel is the bottom of
    # that range, so it lands on the stored nodata rather than needing a
    # separate pass.
    #
    # GDAL rounds on the float-to-Int16 conversion. Were it ever to truncate
    # instead, the cost would be a constant offset of up to one quantisation
    # step — which cancels exactly in every height difference, so slope, the
    # thing this grid is for, would be unaffected either way.
    #
    # ENVI, because the output is meant to be a flat array and nothing else: the
    # cutter slices bytes out of it with no decoder at all.
    gdal_translate \
        -of ENVI \
        -ot Int16 \
        -scale "$s_min" "$s_max" "$d_min" "$d_max" \
        -a_nodata "$d_min" \
        "$warped" "$raw"
fi

# ENVI writes in the host's byte order. Every machine anyone will run this on is
# little-endian, which is what the published tiles claim to be — but a silently
# byte-swapped grid would decode as noise on the Django side rather than fail,
# so it is worth the one grep.
hdr="${raw%.raw}.hdr"
[ -f "$hdr" ] || hdr="${raw}.hdr"
if [ -f "$hdr" ] && ! grep -qi '^byte order *= *0' "$hdr"; then
    echo "error: ${hdr} is not little-endian; the tile format assumes it is" >&2
    exit 1
fi

# --- 5. Cut the tiles --------------------------------------------------------

"$python" scripts/cut_terrain_tiles.py \
    --raster "$raw" \
    --west "$te_w" --north "$te_n" \
    --cols "$cols" --rows "$rows" \
    --out "$out" \
    --origin "$TILES_ORIGIN" \
    --version "$TERRAIN_VERSION" \
    --manifest "$manifest"

echo
echo "==> ${out} ready: $(du -sh "$out" | cut -f1)"
echo "    publish with: op run --env-file=.env.1password -- ./scripts/upload.sh"
echo "    then:         ./scripts/verify.sh"
echo
echo "    ${work} holds ~$(du -sh "$work" 2>/dev/null | cut -f1) of intermediates"
echo "    and can be deleted once the tiles are published."
