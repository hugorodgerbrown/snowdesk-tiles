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
header() { curl -sI "$1" | tr -d '\r' | grep -i "^$2:" | cut -d' ' -f2- | tail -1; }

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
    "$(status "${TILES_ORIGIN}/tiles/0/0/0.mvt")"
check "vector tile is protobuf" "application/x-protobuf" \
    "$(content_type "${TILES_ORIGIN}/tiles/0/0/0.mvt")"
check "tilejson responds" 200 \
    "$(status "${TILES_ORIGIN}/tiles/tiles.json")"

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
    bytes=$(curl -s -o /dev/null -w '%{size_download}' "${TILES_ORIGIN}/tiles/${tile}.mvt")
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
    acao=$(curl -sI -H "Origin: ${site}" "${TILES_ORIGIN}/tiles/0/0/0.mvt" \
        | tr -d '\r' | grep -i '^access-control-allow-origin:' | cut -d' ' -f2-)
    check "CORS allows ${site}" "$site" "${acao:-<none>}"
done

# Not a failure — the origin is correct either way — but DYNAMIC means the edge
# is not caching, so every glyph and style read goes to R2. Cloudflare caches
# .png by extension and nothing else here, so this is the signal that the Cache
# Rule setup-bucket.sh describes has not been added.
echo
for path in "/styles/liberty" "/fonts/Noto%20Sans%20Regular/0-255.pbf"; do
    cache_status=$(header "${TILES_ORIGIN}${path}" "cf-cache-status")
    case "$cache_status" in
        HIT | MISS | EXPIRED | REVALIDATED)
            printf '  ok    edge-cached (%s): %s\n' "$cache_status" "$path"
            ;;
        *)
            printf '  warn  not edge-cached (%s): %s\n' "${cache_status:-none}" "$path"
            printf '        add the Cache Rule — see scripts/setup-bucket.sh\n'
            ;;
    esac
done

echo
if [ "$failures" -gt 0 ]; then
    echo "${failures} check(s) failed"
    exit 1
fi
echo "all checks passed"
