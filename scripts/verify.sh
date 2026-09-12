#!/usr/bin/env bash
# Acceptance checks against the published origin (SNOW-485).
#
# Exits non-zero on the first failure so it can gate a release.
#
#     ./scripts/verify.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

: "${SITE_ORIGIN:=https://snowdesk.info}"

failures=0
check() {
    local label=$1 expected=$2 actual=$3
    if [ "$actual" = "$expected" ]; then
        printf '  ok    %s\n' "$label"
    else
        printf '  FAIL  %s (expected %s, got %s)\n' "$label" "$expected" "$actual"
        failures=$((failures + 1))
    fi
}

status() { curl -so /dev/null -w '%{http_code}' "$@"; }
# -I so this stays a HEAD request. Without it, checking the content type of the
# archive downloads 1.5 GB to read one response header.
content_type() { curl -sI -o /dev/null -w '%{content_type}' "$@"; }
# `|| true` is load-bearing: grep exits 1 when the header is absent, and under
# `set -o pipefail` that kills the whole script — silently, before the summary,
# so a clean run reports failure.
header() { curl -sI "$1" | tr -d '\r' | grep -i "^$2:" | cut -d' ' -f2- | tail -1 || true; }

echo "==> ${TILES_ORIGIN}"

check "style responds" 200 "$(status "${TILES_ORIGIN}/styles/liberty")"

residual=$(curl -s "${TILES_ORIGIN}/styles/liberty" | grep -c "$UPSTREAM_ORIGIN" || true)
check "no residual upstream references in style" 0 "$residual"

# The hard requirement for the client-side PMTiles reader: a byte range must
# come back as 206, not a 200 with the whole multi-GB file.
check "pmtiles serves ranges" 206 \
    "$(status -r 0-1000 "${TILES_ORIGIN}/${PMTILES_NAME}")"

sprite=$(curl -s "${TILES_ORIGIN}/styles/liberty" | sed -n 's/.*"sprite"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
if [ -n "$sprite" ]; then
    check "sprite atlas responds" 200 "$(status "${sprite}.png")"
    check "sprite index responds" 200 "$(status "${sprite}.json")"
else
    echo "  skip  sprite (no string sprite field in style)"
fi

check "glyphs respond" 200 \
    "$(status "${TILES_ORIGIN}/fonts/Noto%20Sans%20Regular/0-255.pbf")"

# The shaded-relief raster is a separate pyramid from the PMTiles archive and is
# easy to forget: the map renders fine at the zoom levels you happen to test and
# 404s the background when zoomed out.
check "natural earth raster responds" 200 \
    "$(status "${TILES_ORIGIN}/natural_earth/ne2sr/0/0/0.png")"

# Vector tiles come from the Worker, not the bucket. Until it is deployed the
# style resolves and every other asset loads while the map renders empty, so
# these are the checks that tell the two states apart.
check "vector tile responds" 200 \
    "$(status "${TILES_ORIGIN}/tiles/${TILE_VERSION}/0/0/0.mvt")"
check "vector tile is protobuf" "application/x-protobuf" \
    "$(content_type "${TILES_ORIGIN}/tiles/${TILE_VERSION}/0/0/0.mvt")"
check "tilejson responds" 200 \
    "$(status "${TILES_ORIGIN}/tiles/${TILE_VERSION}/tiles.json")"

# The style must state the vector source's zoom range, and state it correctly.
# The rewrite drops the upstream `url` that used to carry it in TileJSON, and a
# vector source with no maxzoom defaults to 22 in MapLibre: the client then asks
# for z15+ tiles the archive does not hold (204, empty) instead of overzooming
# z14, and every basemap layer disappears above z14. Nothing else in this suite
# notices — the whole thing passed while the map rendered blank when zoomed in.
#
# Compared against the archive's own header via the Worker's TileJSON rather
# than against config.sh, so a rebuild that changes the range and a style that
# was not rebuilt with it cannot agree by accident.
#
# Parsed with python3 rather than sed: the vector source's own tiles template
# contains `{z}/{x}/{y}`, so any attempt to slice its JSON object on braces
# stops inside the URL. Every other script here already needs an interpreter.
zoom_range() {
    python3 -c '
import json, sys
doc = json.load(sys.stdin)
source = doc.get("sources", {}).get(sys.argv[1], doc) if sys.argv[1] else doc
print(source.get("minzoom", "<unset>"), source.get("maxzoom", "<unset>"))
' "$1" 2>/dev/null || echo "<unreadable> <unreadable>"
}

read -r want_min want_max < <(curl -s "${TILES_ORIGIN}/tiles/${TILE_VERSION}/tiles.json" | zoom_range "")
read -r got_min got_max < <(curl -s "${TILES_ORIGIN}/styles/liberty" | zoom_range openmaptiles)

check "style declares vector minzoom" "$want_min" "$got_min"
check "style declares vector maxzoom" "$want_max" "$got_max"

# The attribution goes the same way as the zoom range if it is not stated: it
# lived in the TileJSON the rewrite drops, and the Snowdesk legend builds its
# "Map data" section by reading `attribution` off each runtime source. With none
# on any source the section renders as a bare heading and the OpenStreetMap and
# OpenMapTiles credits we are obliged to show are simply absent (SNOW-640).
# Nothing else here notices — every asset loads and the map renders correctly.
#
# Compared against the Worker's TileJSON for the same reason as the zoom range:
# both surfaces publish this string, a client can reach either, and comparing
# them to each other is the only way a copy that was not rebuilt shows up.
#
# Not folded into zoom_range: the value contains spaces, so `read -r a b` would
# split it across fields.
attribution() {
    python3 -c '
import json, sys
doc = json.load(sys.stdin)
source = doc.get("sources", {}).get(sys.argv[1], doc) if sys.argv[1] else doc
print(source.get("attribution") or "<unset>")
' "$1" 2>/dev/null || echo "<unreadable>"
}

want_attribution=$(curl -s "${TILES_ORIGIN}/tiles/${TILE_VERSION}/tiles.json" | attribution "")
got_attribution=$(curl -s "${TILES_ORIGIN}/styles/liberty" | attribution openmaptiles)

check "style declares vector attribution" "$want_attribution" "$got_attribution"

# Coverage, at named places rather than in the abstract. A regional extract is
# clipped to a polygon, not a bounding box, so tiles are served across the whole
# bbox and are simply empty outside it — every status check passes while parts
# of Switzerland render blank. Basel returned 0 bytes under the "alps" extract
# and nothing caught it.
#
# z10 tiles carrying real map data are tens of kB; a few hundred bytes means
# boundary lines and nothing else.
: "${MIN_TILE_BYTES:=20000}"
coverage() {
    local label=$1 tile=$2
    local bytes
    bytes=$(curl -s -o /dev/null -w '%{size_download}' "${TILES_ORIGIN}/tiles/${TILE_VERSION}/${tile}.mvt")
    if [ "$bytes" -ge "$MIN_TILE_BYTES" ]; then
        printf '  ok    coverage: %s (%s bytes)\n' "$label" "$bytes"
    else
        printf '  FAIL  coverage: %s is empty or sparse (%s bytes)\n' "$label" "$bytes"
        failures=$((failures + 1))
    fi
}

# Corners and edges of the served area, not a tour of the middle. Basel and
# Ajoie sit in the Swiss north-west, which is exactly what the "alps" polygon
# clipped off while every check around the Alps kept passing.
#
# The Pyrenees and Corsica appear in the region reference data but are not served
# on the map, so they are deliberately not checked here. If that changes, the
# bounding box in config.sh has to change with it — neither is anywhere near
# this box.
coverage "Basel (CH N)" 10/533/357
coverage "Ajoie (CH NW)" 10/532/358
coverage "Geneva (CH W)" 10/529/363
coverage "Zermatt (CH alpine)" 10/534/364
coverage "St Gallen (CH E)" 10/538/358
coverage "Ybbstaler (AT N)" 10/554/356
coverage "Semmering (AT E)" 10/557/357
coverage "Innsbruck (AT)" 10/544/359
coverage "Canin (IT Giulie)" 10/550/362
coverage "Bolzano (IT)" 10/544/362
coverage "Grenoble (FR Alps)" 10/528/367

# --- Terrain elevation grid (SNOW-908) --------------------------------------
#
# Nothing renders these, so none of the failures below would show up on a map.
# They surface as a wrong slope angle on a route line, which is the failure mode
# this whole layer exists to avoid — so they are checked here rather than left
# to be noticed downstream.
#
# TERRAIN=0 skips the section. That is for publishing a basemap-only change
# before the terrain build has ever been run; it is not a way past a failure.
if [ "${TERRAIN:-1}" != "1" ]; then
    echo
    echo "  skip  terrain checks (TERRAIN=0)"
else
    echo
    terrain_url="${TILES_ORIGIN}/terrain/${TERRAIN_VERSION}"

    check "terrain grid definition responds" 200 "$(status "${terrain_url}/grid.json")"
    check "terrain grid definition is json" "application/json" \
        "$(content_type "${terrain_url}/grid.json")"

    # The published definition against the one this repo builds on. This is the
    # contract SNOW-917 decodes tiles with, and the two live in different
    # repositories, so a grid rebuilt with new geometry and a Django side still
    # reading the old numbers would not fail — it would return plausible,
    # silently wrong heights. Comparing the published copy to the committed one
    # is the only place that shows up.
    #
    # Only the geometry and encoding are compared. Coverage, provenance and the
    # URL template legitimately differ between a definition and a build.
    contract() {
        python3 -c '
import json, sys
doc = json.load(sys.stdin)
keys = ["grid", "crs", "cell_size_m", "tile_cells", "skirt_cells",
        "stored_cells", "tile_bytes", "dtype", "byte_order",
        "height_scale_m", "height_offset_m", "nodata", "row_order",
        "column_order"]
print(json.dumps({k: doc.get(k) for k in keys}, sort_keys=True))
' 2>/dev/null || echo "<unreadable>"
    }

    want_grid=$(python3 scripts/terrain_grid.py definition | contract)
    got_grid=$(curl -s "${terrain_url}/grid.json" | contract)
    check "terrain grid matches scripts/terrain_grid.py" "$want_grid" "$got_grid"

    # A tile is a fixed-size array, and the size is the contract. A short body
    # decodes as garbage from the first row rather than failing, so the length
    # is worth asserting on its own.
    tile_bytes=$(python3 -c \
        'import sys; sys.path.insert(0, "scripts"); import terrain_grid; print(terrain_grid.TILE_BYTES)')

    # Named places again, and for the same reason as the vector coverage checks
    # — except that here a wrong answer is a *number*, not a blank screen. A
    # grid that is offset, flipped north-south, or built in the wrong projection
    # still returns a plausible height for every coordinate; only comparing
    # against ground whose elevation is known catches it.
    #
    # Ranges rather than exact values. These assert that the grid is correctly
    # georeferenced, which is this suite's job; they do not assert swissALTI3D's
    # absolute accuracy, which is swisstopo's.
    elevation() {
        local label=$1 lon=$2 lat=$3 low=$4 high=$5
        local tx ty row col body_size height

        read -r tx ty row col < <(python3 scripts/terrain_grid.py cell "$lon" "$lat")
        body_size=$(curl -s -o /dev/null -w '%{size_download}' \
            "${terrain_url}/${tx}/${ty}.s16")
        if [ "$body_size" != "$tile_bytes" ]; then
            printf '  FAIL  terrain: %s tile %s/%s is %s bytes, expected %s\n' \
                "$label" "$tx" "$ty" "$body_size" "$tile_bytes"
            failures=$((failures + 1))
            return
        fi

        height=$(curl -s "${terrain_url}/${tx}/${ty}.s16" \
            | python3 scripts/terrain_grid.py read-cell "$row" "$col")
        if [ "$height" = "nodata" ] || [ "$height" = "short" ]; then
            printf '  FAIL  terrain: %s reads %s, expected %s-%s m\n' \
                "$label" "$height" "$low" "$high"
            failures=$((failures + 1))
        elif awk -v h="$height" -v lo="$low" -v hi="$high" \
            'BEGIN { exit !(h >= lo && h <= hi) }'; then
            printf '  ok    terrain: %s (%s m)\n' "$label" "$height"
        else
            printf '  FAIL  terrain: %s reads %s m, expected %s-%s m\n' \
                "$label" "$height" "$low" "$high"
            failures=$((failures + 1))
        fi
    }

    # Lake surfaces first: flat, unambiguous in a terrain model, and known to
    # the metre, so they catch a vertical datum or scale error that mountains
    # are too rough to show. Then the range of the country, high and low, north
    # to south — a north-south flip puts the Jungfrau's height on the Mittelland
    # and nothing else here would notice.
    elevation "Lake Geneva surface" 6.5983 46.4436 368 376
    elevation "Lake Neuchatel surface" 6.7860 46.8860 425 433
    elevation "Basel (CH N, low)" 7.5886 47.5596 230 320
    elevation "Zermatt village" 7.7481 46.0208 1550 1680
    elevation "Jungfraujoch (CH high)" 7.9806 46.5472 3350 3520
    elevation "Lugano (CH S)" 8.9511 46.0037 260 420

    # The honest-unknown path, and the one the blocked tickets depend on most.
    # Outside swissALTI3D's coverage there is no height, and the caller has to
    # be able to tell that from a valid one — 204, not a 404 and certainly not a
    # zero. SNOW-839 and SNOW-910 both turn on absent never rendering as gentle.
    read -r tx ty _ _ < <(python3 scripts/terrain_grid.py cell 11.3933 47.2692)
    check "terrain is 204 outside coverage (Innsbruck)" 204 \
        "$(status "${terrain_url}/${tx}/${ty}.s16")"

    # A negative index is a caller's arithmetic bug, not missing coverage, and
    # the Worker's route says so. 204 here would let a sign error downstream
    # look exactly like ground nobody has surveyed.
    check "terrain rejects a negative tile index" 404 \
        "$(status "${terrain_url}/-1/-1.s16")"
fi

# Content-Type is fixed at upload time and R2 does not infer it. A wrong one
# fails inside MapLibre rather than at the HTTP layer, so every status check
# above can pass while the map renders nothing.
check "style is json" "application/json" \
    "$(content_type "${TILES_ORIGIN}/styles/liberty")"
check "glyphs are protobuf" "application/x-protobuf" \
    "$(content_type "${TILES_ORIGIN}/fonts/Noto%20Sans%20Regular/0-255.pbf")"
check "archive is binary" "application/octet-stream" \
    "$(content_type "${TILES_ORIGIN}/${PMTILES_NAME}")"
check "raster is png" "image/png" \
    "$(content_type "${TILES_ORIGIN}/natural_earth/ne2sr/0/0/0.png")"

# CORS is what actually breaks in production: a working origin the browser
# refuses to read cross-origin looks identical to an outage from the app side.
#
# Every origin in the allowlist is checked, not just one. Checking a single
# origin is what let the staging origin go missing in the move off Caddy — the
# suite passed while snowdesk-staging.onrender.com was blocked, and it surfaced
# in a browser instead.
#
# The list is read out of worker/wrangler.toml rather than restated here: it is
# the only live copy, and a second one would drift the same way.
allowed=$(sed -n 's/^ALLOWED_ORIGINS *= *"\(.*\)"/\1/p' worker/wrangler.toml)
if [ -z "$allowed" ]; then
    echo "  FAIL  could not read ALLOWED_ORIGINS from worker/wrangler.toml"
    failures=$((failures + 1))
fi

for site in $allowed; do
    acao=$(curl -sI -H "Origin: ${site}" "${TILES_ORIGIN}/tiles/${TILE_VERSION}/0/0/0.mvt" \
        | tr -d '\r' | grep -i '^access-control-allow-origin:' | cut -d' ' -f2-)
    check "CORS allows ${site}" "$site" "${acao:-<none>}"
done

# Edge-cache status is no longer checked here. It used to read cf-cache-status
# to confirm the zone Cache Rule was working, back when the bucket served these
# paths over its own custom domain. The Worker now serves everything and does
# its own caching through the Cache API, and Cloudflare does not stamp
# cf-cache-status on Worker responses — the header is simply absent, so the
# check could only ever have warned.

echo
if [ "$failures" -gt 0 ]; then
    echo "${failures} check(s) failed"
    exit 1
fi
echo "all checks passed"
