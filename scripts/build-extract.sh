#!/usr/bin/env bash
# Build the OpenMapTiles-schema vector tile archive with planetiler (SNOW-485).
#
# The Liberty style expects the OpenMapTiles schema. planetiler emits that
# schema; the ready-made extracts at protomaps.com/extracts use the Protomaps
# schema and will not render with Liberty. Do not substitute them.
#
# Requires Java 21+ and roughly PLANETILER_MEMORY of free RAM. The "alps" area
# takes ~20 minutes on a laptop and produces a 2-4 GB archive.
#
#     ./scripts/build-extract.sh
#     PLANETILER_AREA=europe PMTILES_NAME=europe.pmtiles ./scripts/build-extract.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

jar="planetiler.jar"
jar_url="https://github.com/onthegomap/planetiler/releases/download/v${PLANETILER_VERSION}/planetiler.jar"

if ! command -v java >/dev/null 2>&1; then
    echo "error: java not found; planetiler needs Java 21+" >&2
    exit 1
fi

if [ ! -f "$jar" ]; then
    echo "==> downloading planetiler ${PLANETILER_VERSION}"
    curl -fL --progress-bar -o "$jar" "$jar_url"
fi

mkdir -p "$DIST_DIR"

echo "==> building ${PLANETILER_AREA} -> ${DIST_DIR}/${PMTILES_NAME}"
java "-Xmx${PLANETILER_MEMORY}" -jar "$jar" \
    --download \
    --area="$PLANETILER_AREA" \
    --output="${DIST_DIR}/${PMTILES_NAME}" \
    --force

echo "==> built $(du -h "${DIST_DIR}/${PMTILES_NAME}" | cut -f1) archive"
echo "    inspect it with: pmtiles show ${DIST_DIR}/${PMTILES_NAME}"
