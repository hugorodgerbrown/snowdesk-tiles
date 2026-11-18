#!/usr/bin/env bash
# Composite the two warped terrain sources into one raster (SNOW-1505).
#
#     ./scripts/composite-terrain.sh MAPTERHORN SWISS OUT WEST SOUTH EAST NORTH
#
# MAPTERHORN and SWISS are Float32 rasters already warped onto the 5 m EPSG:3035
# grid over the same extent (build-terrain.sh warps both to the same -te and
# -tr), with the grid's float no-data sentinel declared. OUT is written over
# that extent, Mapterhorn first and swissALTI3D last: gdalwarp writes its inputs
# in order and skips each input's no-data cells, so every cell swissALTI3D holds
# is swissALTI3D's, unchanged, and Mapterhorn fills only where it holds nothing.
#
# -r near and -et 0 because the grids are identical: each output cell takes
# exactly one input cell, with no resampling and no approximated transform to
# move it. The heights inside Switzerland must come out bit for bit as the
# swissALTI3D warp wrote them; compare_grids.py checks that they did.
#
# Requires GDAL (gdalwarp) and Python 3.12+ (for the grid's constants).

set -euo pipefail

if [ "$#" -ne 7 ]; then
    echo "usage: $0 MAPTERHORN SWISS OUT WEST SOUTH EAST NORTH" >&2
    exit 2
fi
mapterhorn=$1 swiss=$2 out=$3
shift 3

script_dir=$(cd "$(dirname "$0")" && pwd)
python=${PYTHON:-python3}
cell=$("$python" "${script_dir}/terrain_grid.py" gdal-args cell-size)
dstnodata=$("$python" "${script_dir}/terrain_grid.py" gdal-args dstnodata)

for input in "$mapterhorn" "$swiss"; do
    [ -s "$input" ] || { echo "error: ${input} is missing or empty" >&2; exit 1; }
done

gdalwarp \
    -te "$1" "$2" "$3" "$4" \
    -tr "$cell" "$cell" \
    -r near \
    -et 0 \
    -ot Float32 \
    -dstnodata "$dstnodata" \
    -multi -wo NUM_THREADS=ALL_CPUS \
    -co TILED=YES -co COMPRESS=DEFLATE -co BIGTIFF=YES \
    -overwrite \
    "$mapterhorn" "$swiss" "$out"
