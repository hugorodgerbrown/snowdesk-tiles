#!/usr/bin/env bash
# Build the terrain elevation tileset (SNOW-908; beyond Switzerland, SNOW-1505).
#
# Fetch swissALTI3D -> reproject to EPSG:3035 -> resample to 5 m -> quantise to
# Int16 -> cut skirted tiles into dist/terrain/. Then ./scripts/upload.sh.
#
# Outside Switzerland the grid is filled from our elevation copy (dem/<version>/
# on TILES_ORIGIN, SNOW-1444): its z14 Terrarium tiles over the supported area
# inside TERRAIN_MAPTERHORN_BBOX are decoded, warped onto the same grid and
# clipped to the coverage polygons of the national models Mapterhorn holds, so
# ground only Copernicus GLO-30 covers stays absent (204). swissALTI3D is warped
# exactly as before and composited last, so every Swiss height is unchanged.
# Before anything is downloaded in bulk, datum_check.py compares heights across
# four border crossings and stops the build on a step over 3 m.
#
#     ./scripts/build-terrain.sh
#     TERRAIN_BBOX="7.70 45.98 7.80 46.05" ./scripts/build-terrain.sh   # smoke test
#
# TERRAIN_MAPTERHORN_BBOX empty builds swissALTI3D alone, by exactly v1's path:
#
#     TERRAIN_MAPTERHORN_BBOX= TERRAIN_VERSION=v1 ./scripts/build-terrain.sh
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
# Requires GDAL (gdalbuildvrt, gdalwarp, gdal_translate; and gdallocationinfo,
# plus ogr2ogr for a GeoPackage coverage file, for the Mapterhorn fill) and
# Python 3.12+, with numpy and Pillow for the Mapterhorn fill.
#
#     Debian/Ubuntu:  sudo apt-get install -y gdal-bin python3 python3-numpy python3-pil
#     macOS:          brew install gdal && pip install '.[build]'
#
# Sizing, measured over all of Switzerland on 2026-09-13: 43,650 source GeoTIFFs
# at about 1 MB each (~44 GB), a warped intermediate and a 7.1 GB flat grid —
# 56 GB of work/terrain all told — and 3.6 GB of tiles (27,331 of them, out of
# 53,760 slots). Budget 100 GB free and about 90 minutes on 8
# vCPU — the warp is the longest single stage, and the download is the only one
# that scales with the number of source squares rather than with area. A 66 km2
# box around Zermatt runs end to end in 22 seconds.
#
# With the Mapterhorn fill over the Alps (estimated, not yet measured): a grid
# of ~184,576 x 103,936 cells, ~38 GB as flat Int16, beside the decoded z14
# blocks (4 bytes a pixel), the Mapterhorn and merged Float32 rasters and the
# tiles — peak intermediates of roughly 300-350 GB. Budget 400 GB free.
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
# shellcheck source=scripts/failure.sh
source scripts/failure.sh

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

# Empty TERRAIN_MAPTERHORN_BBOX is the swissALTI3D-only build, v1's exact path.
if [ -n "${TERRAIN_MAPTERHORN_BBOX:-}" ]; then
    mapterhorn=1
else
    mapterhorn=0
fi

tools="gdalbuildvrt gdalwarp gdal_translate curl"
[ "$mapterhorn" = 1 ] && tools="$tools gdallocationinfo gdal_rasterize ogr2ogr gdal_polygonize.py"
for tool in $tools; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: ${tool} not found — install GDAL (apt install gdal-bin, brew install gdal)" >&2
        exit 1
    }
done
if [ "$mapterhorn" = 1 ] && ! "$python" -c 'import numpy, PIL' 2>/dev/null; then
    cat >&2 <<EOF
error: ${python} cannot import numpy and Pillow, which the Mapterhorn fill needs

    sudo apt-get install -y python3-numpy python3-pil     # Debian/Ubuntu
    pip install '.[build]'                                # anywhere else

or build swissALTI3D alone with TERRAIN_MAPTERHORN_BBOX= (empty).
EOF
    exit 1
fi

# --- Preflight ---------------------------------------------------------------
#
# Everything the run will need, checked before the first long stage, so a
# missing tool, an unreachable host or a small box is said in the first minute
# rather than hours in. Each check prints a line; any failure stops the run.

stage "the preflight checks" \
    "Something the build needs is missing or unreachable; the line marked FAIL above names it. Nothing has been downloaded in bulk or uploaded." \
    "Fix what the FAIL line names, or send Claude the box and the lines above it."
echo "==> preflight"
preflight_failed=0
check_url() {
    local label=$1 url=$2
    if curl -fsS -o /dev/null --max-time 30 --retry 2 --range 0-0 "$url" 2>/dev/null; then
        echo "    ok    ${label}"
    else
        echo "    FAIL  ${label}: cannot read ${url} from this machine" >&2
        preflight_failed=1
    fi
}
if [ "$mapterhorn" = 1 ] && ! gdal_polygonize.py --help >/dev/null 2>&1; then
    echo "    FAIL  gdal_polygonize.py does not run: install python3-gdal (apt install python3-gdal)" >&2
    preflight_failed=1
fi
stac_url=$("$python" -c 'import sys; sys.path.insert(0, "scripts"); import fetch_swissalti3d as f; print(f.COLLECTION_URL)')
check_url "swisstopo's swissALTI3D catalogue" "$stac_url"
if [ "$mapterhorn" = 1 ]; then
    check_url "our elevation copy dem/${TERRAIN_DEM_VERSION}" "${TILES_ORIGIN}/${DEM_DIR}/${TERRAIN_DEM_VERSION}/manifest.json"
    check_url "Mapterhorn's attribution.json" "$TERRAIN_MAPTERHORN_ATTRIBUTION"
    if [ "$TERRAIN_MAPTERHORN_COVERAGE" = tarballs ]; then
        mirror=$("$python" -c 'import sys; sys.path.insert(0, "scripts"); import mapterhorn_sources as m; print(m.TARBALL_BASE)')
        check_url "the source.coop mirror of Mapterhorn's tarballs" "${mirror}/at1.tar"
    fi
    mem_gb=$(awk '/^MemTotal:/ {print int($2 / 1048576)}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "${mem_gb:-0}" -gt 0 ] && [ "$mem_gb" -lt 12 ]; then
        # A warning, not a stop: the compact step ran in 15 GB on 19 Nov 2026,
        # and the true floor was not measured.
        echo "    warn  ${mem_gb} GB of memory; compacting the clip outline ran in 15 GB, so it may run out (CPX62 has 32)" >&2
    elif [ "${mem_gb:-0}" -gt 0 ]; then
        echo "    ok    ${mem_gb} GB of memory"
    fi
fi
if [ "$preflight_failed" = 1 ]; then
    exit 1
fi

work=$TERRAIN_WORK_DIR
tif_dir="${work}/tif"
urls="${work}/urls.txt"
manifest="${work}/manifest.json"
vrt="${work}/source.vrt"
warped="${work}/warped.tif"
raw="${work}/grid.raw"
out="${DIST_DIR}/${TERRAIN_DIR}"

# The Mapterhorn fill's working files. The swissALTI3D fetch manifest is kept
# apart from manifest.json, which becomes the widened one every later stage
# (and build-terrain-class.sh) reads the extent from; swissALTI3D's published
# coverage still comes from its own extent.
# Keyed by the elevation copy's version, so a rerun against another copy never
# decodes tiles fetched from the old one under the new copy's provenance.
m_dir="${work}/mapterhorn/${TERRAIN_DEM_VERSION}"
dem_url="${TILES_ORIGIN}/${DEM_DIR}/${TERRAIN_DEM_VERSION}"
if [ "$mapterhorn" = 1 ]; then
    swiss_manifest="${work}/swissalti3d-manifest.json"
else
    swiss_manifest=$manifest
fi
mapterhorn_tif="${work}/mapterhorn.tif"
merged="${work}/merged.tif"

mkdir -p "$tif_dir" "$out"
[ "$mapterhorn" = 1 ] && mkdir -p "$m_dir"

# Not fatal — a single region needs a fraction of this, and the check cannot
# know which you are building. Wrong is better said early than at 90%.
free_gb=$(df -Pk "$work" | awk 'NR==2 {print int($4 / 1048576)}')
if [ "$mapterhorn" = 1 ] && [ "${free_gb:-0}" -lt 350 ]; then
    echo "warning: ${free_gb} GB free; the Alps-wide grid needs ~300-350 GB of" >&2
    echo "         intermediates and tiles, so budget 400 GB" >&2
elif [ "${free_gb:-0}" -lt 80 ]; then
    echo "warning: ${free_gb} GB free; all of Switzerland needs ~60 GB of" >&2
    echo "         intermediates and tiles, so budget 100 GB" >&2
fi

# --- 0. Mapterhorn: copy, coverage, credits and the datum stop ----------------
#
# All of it before the bulk download, so a copy cut too shallow, a source with
# no credit, or a model on a different height reference stops the build while
# it has cost minutes rather than hours. There is no flag to skip any of it.

if [ "$mapterhorn" = 1 ]; then
    stage "reading the elevation copy's manifest" \
        "The server could not read dem/${TERRAIN_DEM_VERSION}/manifest.json from ${TILES_ORIGIN}, or the copy stops below zoom 14. Either step 6 (the dem build) did not upload it, or the network is down. Nothing has been uploaded." \
        "Check it from the server: curl -sI ${dem_url}/manifest.json should say 200. If it says 404, rerun step 6. Otherwise send Claude the box."
    echo "==> elevation copy ${dem_url}"
    curl -fsS "${dem_url}/manifest.json" -o "${m_dir}/dem-manifest.json"
    "$python" scripts/mapterhorn_sources.py dem-zoom "${m_dir}/dem-manifest.json"

    stage "reading Mapterhorn's attribution and coverage" \
        "A download from Mapterhorn or its source.coop mirror failed or was refused, or a coverage file would not convert. Nothing has been uploaded." \
        "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
    echo "==> Mapterhorn attribution and coverage"
    curl -fsS "$TERRAIN_MAPTERHORN_ATTRIBUTION" -o "${m_dir}/attribution.json"
    coverage=$TERRAIN_MAPTERHORN_COVERAGE
    if [ "$coverage" = tarballs ]; then
        # Mapterhorn publishes no single coverage file; each source tarball
        # opens with one. Read just those, over HTTP ranges.
        # Rebuilt every run, from the GeoPackages kept in coverage/, so a
        # changed bbox never reuses polygons filtered to the old one.
        coverage="${m_dir}/coverage.geojson"
        # shellcheck disable=SC2086 - four numbers
        "$python" scripts/mapterhorn_sources.py coverage \
            --attribution "${m_dir}/attribution.json" \
            --bbox $TERRAIN_MAPTERHORN_BBOX \
            --work "${m_dir}/coverage" \
            --out "${coverage}.part"
        mv "${coverage}.part" "$coverage"
    fi
    case $coverage in
        http://* | https://*)
            fetched="${m_dir}/$(basename "${coverage%%\?*}")"
            [ -s "$fetched" ] || curl -fsS "$coverage" -o "$fetched"
            coverage=$fetched
            ;;
    esac
    if [ ! -s "$coverage" ]; then
        echo "error: no Mapterhorn coverage file at ${coverage}" >&2
        exit 1
    fi
    case $coverage in
        *.gpkg)
            command -v ogr2ogr >/dev/null 2>&1 || {
                echo "error: ogr2ogr not found — install GDAL to read ${coverage}" >&2
                exit 1
            }
            coverage_json="${m_dir}/coverage-converted.geojson"
            ogr2ogr -f GeoJSON -t_srs EPSG:4326 -overwrite "$coverage_json" "$coverage"
            ;;
        *) coverage_json=$coverage ;;
    esac

    stage "choosing the national models" \
        "The coverage file holds no national model over the supported area, or a model has no credit in attribution.json, so its heights could not be published. Nothing has been uploaded." \
        "Send Claude the box and the 20 lines above it. Do not edit anything on the server."
    # shellcheck disable=SC2086 - TERRAIN_MAPTERHORN_BBOX is four numbers
    "$python" scripts/mapterhorn_sources.py select \
        --coverage "$coverage_json" \
        --attribution "${m_dir}/attribution.json" \
        --area "$DEM_AREA" \
        --bbox $TERRAIN_MAPTERHORN_BBOX \
        --clip "${m_dir}/clip-full.geojson" \
        --credits "${m_dir}/credits.json"

    # The polygons as published are far too heavy to cut with: over the Alps
    # they hold 17 million points (676 MB), GDAL refuses to read them as one
    # GeoJSON object, and even when told to, gdalwarp spent ten minutes on one
    # 300 m box and then hit a topology error. So trace one compact outline
    # instead: burn them onto a ~22 m grid, trace that and simplify by half a
    # cell. The borders between national models vanish in the union, so the
    # only edges left are against GLO-30-only ground and the sea, and those
    # move by about one cell (~20 m). Measured on the 18 Nov 2026 coverage:
    # about 7 minutes on 4 cores, 5 MB out, and a 300 m box warps in 4 s.
    stage "compacting the clip outline" \
        "GDAL could not rasterise or trace the coverage polygons. The usual causes are memory (the polygons are about 0.8 GB; free -g shows what is free) or a missing gdal_polygonize.py. Nothing has been uploaded." \
        "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
    echo "==> compacting the clip outline"
    # Over the fill's bbox and every datum crossing's window, since the check
    # runs at all of them whatever the bbox.
    # shellcheck disable=SC2086 - four numbers
    clip_extent=$("$python" scripts/datum_check.py --print-extent $TERRAIN_MAPTERHORN_BBOX)
    # shellcheck disable=SC2086 - four numbers
    OGR_GEOJSON_MAX_OBJ_SIZE=0 gdal_rasterize -q \
        -burn 1 -ot Byte -init 0 -a_nodata 0 \
        -te $clip_extent -tr 0.0003 0.0002 \
        -co TILED=YES -co COMPRESS=DEFLATE \
        "${m_dir}/clip-full.geojson" "${m_dir}/clip-mask.tif"
    rm -f "${m_dir}/clip-traced.gpkg"
    gdal_polygonize.py -q "${m_dir}/clip-mask.tif" -f GPKG "${m_dir}/clip-traced.gpkg" clip
    rm -f "${m_dir}/clip.geojson.part"
    ogr2ogr -f GeoJSON -nln clip -simplify 0.0001 \
        -dialect sqlite -sql "SELECT ST_Union(geom) AS geometry FROM clip" \
        "${m_dir}/clip.geojson.part" "${m_dir}/clip-traced.gpkg"
    mv "${m_dir}/clip.geojson.part" "${m_dir}/clip.geojson"
    echo "    $(du -h "${m_dir}/clip.geojson" | cut -f1) from $(du -h "${m_dir}/clip-full.geojson" | cut -f1)"

    stage "the datum check at the border crossings" \
        "The lines just above say what each crossing found and what it means. Nothing has been uploaded." \
        "Follow the advice printed above the box."
    echo "==> datum check at the border crossings"
    "$python" scripts/datum_check.py \
        --work "${work}/datum/${TERRAIN_DEM_VERSION}" \
        --dem-url "$dem_url" \
        --clip "${m_dir}/clip.geojson" \
        --gsd "$TERRAIN_GSD"
fi

# --- 1. Which squares --------------------------------------------------------

stage "listing swissALTI3D squares" \
    "swisstopo's STAC service did not answer or answered with an error. Nothing has been uploaded." \
    "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
if [ -s "$urls" ] && [ -s "$swiss_manifest" ]; then
    echo "==> ${urls} already listed ($(wc -l <"$urls" | tr -d ' ') squares)"
else
    echo "==> listing swissALTI3D squares over ${TERRAIN_BBOX}"
    # shellcheck disable=SC2086 - TERRAIN_BBOX is four numbers, deliberately split
    "$python" scripts/fetch_swissalti3d.py \
        --bbox $TERRAIN_BBOX \
        --gsd "$TERRAIN_GSD" \
        --urls "$urls" \
        --manifest "$swiss_manifest"
fi

# --- 2. Download -------------------------------------------------------------
#
# Tens of thousands of small GeoTIFFs. curl per file with xargs -P rather than
# anything cleverer, because the only property that matters is that a run
# interrupted at 80% resumes at 80%: a file is downloaded to .part and renamed,
# so a half-written file is never mistaken for a finished one.

stage "downloading swissALTI3D" \
    "Some GeoTIFFs did not download: usually a network blip at swisstopo's end. Nothing has been uploaded." \
    "Run the same command again. It downloads only what is missing."
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

# --- 2b. Mapterhorn: fetch and decode the z14 tiles ---------------------------
#
# The same resumable pattern as the GeoTIFFs. Our origin answers 204 for a tile
# outside the copy; that is kept as an empty file, which the decode reads as no
# data and a rerun does not fetch again.

if [ "$mapterhorn" = 1 ]; then
    stage "fetching the z14 elevation tiles" \
        "Some tiles did not download from ${dem_url}: usually a network blip. Nothing has been uploaded." \
        "Run the same command again. It fetches only what is missing."
    tile_list="${m_dir}/tiles.txt"
    if [ ! -s "$tile_list" ]; then
        # shellcheck disable=SC2086 - four numbers
        "$python" scripts/terrarium.py list "$DEM_AREA" --zoom 14 \
            --bbox $TERRAIN_MAPTERHORN_BBOX >"${tile_list}.part"
        mv "${tile_list}.part" "$tile_list"
    fi
    want_tiles=$(wc -l <"$tile_list" | tr -d ' ')
    echo "==> fetching ${want_tiles} z14 tiles from ${dem_url}"
    export TERRAIN_DEM_URL="$dem_url" TERRAIN_WEBP_DIR="${m_dir}/webp"
    fetch_tile() {
        local zxy=$1 dest
        dest="${TERRAIN_WEBP_DIR}/${zxy}.webp"
        [ -e "$dest" ] && return 0
        mkdir -p "$(dirname "$dest")"
        curl -fsS --retry 3 --retry-delay 2 -o "${dest}.part" "${TERRAIN_DEM_URL}/${zxy}.webp" || {
            echo "error: failed ${TERRAIN_DEM_URL}/${zxy}.webp" >&2
            return 1
        }
        # A 204 writes nothing; make the empty file so the rerun skips it.
        [ -e "${dest}.part" ] || : >"${dest}.part"
        mv "${dest}.part" "$dest"
    }
    export -f fetch_tile
    xargs -P "$TERRAIN_JOBS" -I{} bash -c 'fetch_tile "$@"' _ {} <"$tile_list" || true

    have_tiles=$(find "${m_dir}/webp" -name '*.webp' | wc -l | tr -d ' ')
    if [ "$have_tiles" -ne "$want_tiles" ]; then
        echo "error: ${have_tiles} of ${want_tiles} z14 tiles fetched — rerun to resume" >&2
        exit 1
    fi

    stage "decoding the elevation tiles" \
        "Usually the disk is full (df -h / shows it) or a file was cut short by an earlier stop. Nothing has been uploaded." \
        "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
    mosaic="${m_dir}/blocks/mosaic.vrt"
    if [ -s "$mosaic" ] && [ "${FORCE_TERRAIN:-0}" != "1" ]; then
        echo "==> ${mosaic} exists — skipping decode; FORCE_TERRAIN=1 to redo"
    else
        echo "==> decoding Terrarium into Float32 blocks"
        "$python" scripts/terrarium.py decode \
            --tiles "$tile_list" \
            --tile-dir "${m_dir}/webp" \
            --out "${m_dir}/blocks"
    fi

    # The grid's extent is read from manifest.json, so widening it here is what
    # puts the rest of the Alps on the grid. Rewritten every run: it is cheap,
    # and a stale one would warp the wrong extent.
    echo "==> widening ${manifest} to the Mapterhorn area"
    # shellcheck disable=SC2086 - four numbers
    "$python" scripts/mapterhorn_sources.py manifest \
        --swiss "$swiss_manifest" \
        --area "$DEM_AREA" \
        --bbox $TERRAIN_MAPTERHORN_BBOX \
        --credits "${m_dir}/credits.json" \
        --dem-manifest "${m_dir}/dem-manifest.json" \
        --dem-version "$TERRAIN_DEM_VERSION" >"${manifest}.part"
    mv "${manifest}.part" "$manifest"
fi

stage "reprojecting swissALTI3D (the slow part)" \
    "Usually the disk is full (df -h / shows it) or a file was cut short by an earlier stop. Nothing has been uploaded." \
    "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
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

# A raster is reused only if it was made over this extent: a warped.tif left by
# a Swiss-only run is a different shape from the one an Alps-wide run needs.
stamp="${te_w} ${te_s} ${te_e} ${te_n}"
current() { [ -s "$1" ] && [ "$(cat "${1}.extent" 2>/dev/null)" = "${2:-$stamp}" ] && [ "${FORCE_TERRAIN:-0}" != "1" ]; }
# The Mapterhorn rasters also depend on which elevation copy filled them.
m_stamp="${stamp} dem=${TERRAIN_DEM_VERSION}"

if current "$warped"; then
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
    echo "$stamp" >"${warped}.extent"
fi

stage "warping and compositing the Mapterhorn fill" \
    "Usually the disk is full (df -h / shows it) or a file was cut short by an earlier stop. Nothing has been uploaded." \
    "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
# --- 3b. Mapterhorn: warp, clip and composite --------------------------------

grid_source=$warped
if [ "$mapterhorn" = 1 ]; then
    if current "$mapterhorn_tif" "$m_stamp"; then
        echo "==> ${mapterhorn_tif} exists — skipping warp; FORCE_TERRAIN=1 to redo"
    else
        echo "==> warping the Mapterhorn fill to EPSG:3035 at ${cell} m"
        # The same -te, -tr, resample and no-data as the swissALTI3D warp, so the
        # two land on identical cells. -cutline is the union of the national
        # models' coverage polygons, used as published: GLO-30-only ground is
        # cut away and stays no data.
        gdalwarp \
            -t_srs EPSG:3035 \
            -te "$te_w" "$te_s" "$te_e" "$te_n" \
            -tr "$cell" "$cell" \
            -r average \
            -ot Float32 \
            -dstnodata "$dstnodata" \
            -cutline "${m_dir}/clip.geojson" \
            -multi -wo NUM_THREADS=ALL_CPUS \
            -co TILED=YES -co COMPRESS=DEFLATE -co BIGTIFF=YES \
            -overwrite \
            "$mosaic" "$mapterhorn_tif"
        echo "$m_stamp" >"${mapterhorn_tif}.extent"
    fi

    if current "$merged" "$m_stamp" && [ "$merged" -nt "$warped" ] && [ "$merged" -nt "$mapterhorn_tif" ]; then
        echo "==> ${merged} exists — skipping composite; FORCE_TERRAIN=1 to redo"
    else
        echo "==> compositing: Mapterhorn first, swissALTI3D last"
        ./scripts/composite-terrain.sh "$mapterhorn_tif" "$warped" "$merged" \
            "$te_w" "$te_s" "$te_e" "$te_n"
        echo "$m_stamp" >"${merged}.extent"
    fi
    grid_source=$merged
fi

stage "quantising the grid" \
    "Usually the disk is full (df -h / shows it) or a file was cut short by an earlier stop. Nothing has been uploaded." \
    "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
# --- 4. Quantise to the stored Int16 scale -----------------------------------

if current "$raw" && [ "$(cat "${raw}.source" 2>/dev/null)" = "$grid_source" ] \
    && [ "$raw" -nt "$grid_source" ]; then
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
        "$grid_source" "$raw"
    echo "$stamp" >"${raw}.extent"
    echo "$grid_source" >"${raw}.source"
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

stage "cutting the tiles" \
    "Usually the disk is full (df -h / shows it) or a file was cut short by an earlier stop. Nothing has been uploaded." \
    "Run the same command again: every stage resumes. If it stops here twice, send Claude the box and the 20 lines above it."
# --- 5. Cut the tiles --------------------------------------------------------

"$python" scripts/cut_terrain_tiles.py \
    --raster "$raw" \
    --west "$te_w" --north "$te_n" \
    --cols "$cols" --rows "$rows" \
    --out "$out" \
    --origin "$TILES_ORIGIN" \
    --version "$TERRAIN_VERSION" \
    --manifest "$manifest"

stage "writing what verify.sh should find" \
    "The tiles are built, but working out the gap probe and seam profiles failed. Nothing has been uploaded." \
    "Send Claude the box and the 20 lines above it."
# --- 6. Mapterhorn: what verify.sh should find --------------------------------
#
# Coverage-derived expectations, written beside the tiles for verify.sh: a gap
# probe (inside the supported area, outside every national model: 204) and, for
# each seam profile, where swissALTI3D stops along it — read off the Swiss warp
# itself — and which stretch a national model covers. upload.sh leaves it out of
# the bucket; it is an input to the checks, not a published object.

if [ "$mapterhorn" = 1 ]; then
    "$python" scripts/terrain_grid.py profile-points >"${m_dir}/profile-points.txt"
    cut -f2 "${m_dir}/profile-points.txt" | awk '{print $4, $5}' \
        | gdallocationinfo -valonly -geoloc "$warped" >"${m_dir}/profile-swiss.txt"
    # shellcheck disable=SC2086 - four numbers
    "$python" scripts/mapterhorn_sources.py verify-gap \
        --clip "${m_dir}/clip.geojson" \
        --area "$DEM_AREA" \
        --bbox $TERRAIN_MAPTERHORN_BBOX \
        --swiss "$swiss_manifest" \
        --profile-points "${m_dir}/profile-points.txt" \
        --profile-swiss "${m_dir}/profile-swiss.txt" >"${out}/verify-gap.json"
    echo "==> ${out}/verify-gap.json written for verify.sh"
fi

echo
echo "==> ${out} ready: $(du -sh "$out" | cut -f1)"
echo "    publish with: ./scripts/upload.sh"
echo "    then:         ./scripts/verify.sh"
echo
echo "    ${work} holds ~$(du -sh "$work" 2>/dev/null | cut -f1) of intermediates"
echo "    and can be deleted once the tiles are published."
