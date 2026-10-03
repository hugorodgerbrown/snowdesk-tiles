#!/usr/bin/env bash
# Build the terrain-class tileset from the elevation grid (SNOW-987).
#
# grid.raw -> classify every 5 m cell (height, slope band, aspect octant) in
# EPSG:3035 -> warp to Web Mercator at z12, z13, z14 -> cut PNG tiles into
# dist/terrain-class/. Then ./scripts/upload.sh.
#
#     ./scripts/build-terrain-class.sh
#
# Runs after build-terrain.sh and reads what it left in TERRAIN_WORK_DIR:
# grid.raw, the flat Int16 grid the published .s16 tiles were cut from, and
# manifest.json, which names the ground it covers. Reading grid.raw rather than
# the Float32 warped.tif is deliberate — it is the stored heights, to the
# quarter-metre, so a class computed from it is the class sample_slope computes
# from the published tiles. See scripts/classify_terrain.py.
#
# The order is classify-then-warp, never the reverse: sample_slope differences
# cells on the 3035 grid, and a slope computed on Mercator pixels would be a
# different number that no point sample reproduces.
#
# Requires GDAL (gdalwarp), Python 3.12+ and numpy — the one numpy script in
# the repo is the classifier.
#
#     Debian/Ubuntu:  sudo apt-get install -y gdal-bin python3 python3-numpy
#     macOS:          brew install gdal && pip install numpy
#
# Sizing for all of Switzerland, estimated rather than measured: ~10.6 GB of
# class planes (three bytes per grid cell), ~10 GB of Mercator rasters across
# the three zooms (four bytes per pixel, z14 dominating), and somewhere around
# 1-2 GB of PNGs in ~20,000 tiles. Budget ~25 GB free on top of work/terrain.
# The build prints the real tile count and byte total when it finishes.
#
# Every stage is skipped if its output is already there, so an interrupted run
# resumes; FORCE_TERRAIN_CLASS=1 redoes them.

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

# PYTHON must be a path to an interpreter, not a command line — it is invoked
# quoted, so "uv run python" would be looked up as a single executable name.
python=${PYTHON:-python3}

if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "error: ${python} is not a usable Python 3.12+ interpreter" >&2
    exit 1
fi
if ! "$python" -c 'import numpy' 2>/dev/null; then
    cat >&2 <<EOF
error: ${python} cannot import numpy, which the classifier needs

    sudo apt-get install -y python3-numpy     # Debian/Ubuntu
    pip install '.[build]'                    # anywhere else
EOF
    exit 1
fi
command -v gdalwarp >/dev/null 2>&1 || {
    echo "error: gdalwarp not found — install GDAL (apt install gdal-bin, brew install gdal)" >&2
    exit 1
}

raw="${TERRAIN_WORK_DIR}/grid.raw"
manifest="${TERRAIN_WORK_DIR}/manifest.json"
work=$TERRAIN_CLASS_WORK_DIR
vrt="${work}/classes.vrt"
out="${DIST_DIR}/${TERRAIN_CLASS_DIR}"
force=${FORCE_TERRAIN_CLASS:-0}

if [ ! -s "$raw" ] || [ ! -s "$manifest" ]; then
    cat >&2 <<EOF
error: ${raw} or ${manifest} is missing

The class tileset is derived from the elevation grid. Build that first:

    ./scripts/build-terrain.sh
EOF
    exit 1
fi

mkdir -p "$work" "$out"

free_gb=$(df -Pk "$work" | awk 'NR==2 {print int($4 / 1048576)}')
if [ "${free_gb:-0}" -lt 25 ]; then
    echo "warning: ${free_gb} GB free; all of Switzerland needs ~25 GB of" >&2
    echo "         class planes, Mercator rasters and tiles" >&2
fi

# The grid's extent and shape, from the same call build-terrain.sh made, so the
# planes are georeferenced on exactly the cells grid.raw holds.
read -r te_w te_s te_e te_n _ _ _ _ \
    < <("$python" scripts/terrain_grid.py extent --manifest "$manifest")
cell=$("$python" scripts/terrain_grid.py gdal-args cell-size)
cols=$(( (te_e - te_w) / cell ))
rows=$(( (te_n - te_s) / cell ))

# --- 1. Classify, in EPSG:3035 -----------------------------------------------

if [ -s "$vrt" ] && [ "$force" != "1" ]; then
    echo "==> ${vrt} exists — skipping classification; FORCE_TERRAIN_CLASS=1 to redo"
else
    echo "==> classifying the ${cols}x${rows} grid at the 10 m window"
    "$python" scripts/classify_terrain.py \
        --raster "$raw" \
        --west "$te_w" --north "$te_n" \
        --cols "$cols" --rows "$rows" \
        --out "$work"
fi

# --- 2. Warp to Web Mercator, one raster per zoom -----------------------------

zooms=$(seq "$TERRAIN_CLASS_MIN_ZOOM" "$TERRAIN_CLASS_MAX_ZOOM")
for zoom in $zooms; do
    name="mercator-z${zoom}.raw"
    if [ -s "${work}/${name}" ] && [ "$force" != "1" ]; then
        echo "==> z${zoom}: ${name} exists — skipping warp"
        continue
    fi

    # The pixel grid comes from terrain_class.py, the same function the cutter
    # slices with, so the two cannot disagree about where a tile starts.
    read -r m_w m_s m_e m_n width height _ _ tiles_x tiles_y \
        < <("$python" scripts/terrain_class.py mercator-extent \
            --manifest "$manifest" --zoom "$zoom")
    echo "==> z${zoom}: warping to ${width}x${height} px (${tiles_x}x${tiles_y} tiles)"

    # -r near, and nothing else. Every byte is a code — a height byte, a class
    # — and averaging two classes produces a third that describes neither
    # cell. Nearest takes the one 5 m cell under each pixel's centre.
    #
    # -et 0 forces the exact transformer. The default approximates the
    # reprojection to within an eighth of a pixel, which is invisible in an
    # image and here would move the odd pixel onto the neighbouring cell — and
    # verify.sh compares pixels with sample_slope at the exact cell.
    #
    # -dstalpha with no nodata anywhere: 255 is a class byte, so no value can
    # be spared to mean "no source here". The alpha band says it instead, and
    # INIT_DEST=0 makes every uncovered pixel all zeros, which is what the
    # cutter rewrites to the contract's no-data pixel.
    #
    # ENVI with INTERLEAVE=BIP is a flat RGBA array — exactly the byte layout of
    # a PNG row, so the cutter slices bytes with no decoder at all.
    tmp="${work}/.warp-z${zoom}"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    gdalwarp \
        -t_srs EPSG:3857 \
        -te "$m_w" "$m_s" "$m_e" "$m_n" \
        -ts "$width" "$height" \
        -r near \
        -et 0 \
        -ot Byte \
        -dstalpha \
        -wo INIT_DEST=0 \
        -multi -wo NUM_THREADS=ALL_CPUS -wm 1024 \
        -of ENVI -co INTERLEAVE=BIP \
        "$vrt" "${tmp}/${name}"

    hdr="${tmp}/${name%.raw}.hdr"
    [ -f "$hdr" ] || hdr="${tmp}/${name}.hdr"
    if [ -f "$hdr" ] && ! grep -qi '^interleave *= *bip' "$hdr"; then
        echo "error: ${hdr} is not pixel-interleaved; the cutter assumes RGBA per pixel" >&2
        exit 1
    fi
    # Moved into place only once whole, so a warp interrupted half way is
    # redone rather than mistaken for a finished raster on the next run.
    mv "${tmp}/${name}" "${work}/${name}"
    rm -rf "$tmp"
done

# --- 3. Cut the tiles ---------------------------------------------------------

# shellcheck disable=SC2086 - zooms is a list of integers, deliberately split
"$python" scripts/cut_terrain_class_tiles.py \
    --work "$work" \
    --manifest "$manifest" \
    --out "$out" \
    --origin "$TILES_ORIGIN" \
    --version "$TERRAIN_CLASS_VERSION" \
    --zooms $zooms

echo
echo "==> ${out} ready: $(du -sh "$out" | cut -f1)"
echo "    deploy the Worker first if it predates SNOW-987: cd worker && npx wrangler deploy"
echo "    publish with: op run --env-file=.env.1password -- ./scripts/upload.sh"
echo "    then:         ./scripts/verify.sh"
echo
echo "    ${work} holds ~$(du -sh "$work" 2>/dev/null | cut -f1) of intermediates"
echo "    and can be deleted once the tiles are published."
