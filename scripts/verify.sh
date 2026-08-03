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

# CORS is what actually breaks in production: a working origin that the browser
# refuses to read cross-origin looks identical to an outage from the app side.
cors=$(curl -s -o /dev/null -D - \
    -H "Origin: ${SITE_ORIGIN}" \
    -H 'Range: bytes=0-100' \
    "${TILES_ORIGIN}/${PMTILES_NAME}" | grep -ci 'access-control-allow-origin' || true)
check "CORS header present for ${SITE_ORIGIN}" 1 "$cors"

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
