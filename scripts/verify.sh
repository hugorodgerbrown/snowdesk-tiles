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

# CORS is what actually breaks in production: a working origin that the browser
# refuses to read cross-origin looks identical to an outage from the app side.
cors=$(curl -s -o /dev/null -D - \
    -H "Origin: ${SITE_ORIGIN}" \
    -H 'Range: bytes=0-100' \
    "${TILES_ORIGIN}/${PMTILES_NAME}" | grep -ci 'access-control-allow-origin' || true)
check "CORS header present for ${SITE_ORIGIN}" 1 "$cors"

echo
if [ "$failures" -gt 0 ]; then
    echo "${failures} check(s) failed"
    exit 1
fi
echo "all checks passed"
