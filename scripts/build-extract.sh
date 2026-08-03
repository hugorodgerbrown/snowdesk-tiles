#!/usr/bin/env bash
# Build the OpenMapTiles-schema vector tile archive with planetiler (SNOW-485).
#
# The Liberty style expects the OpenMapTiles schema. planetiler emits that
# schema; the ready-made extracts at protomaps.com/extracts use the Protomaps
# schema and will not render with Liberty. Do not substitute them.
#
# Requires Java 21+ and roughly PLANETILER_MEMORY of free RAM.
#
# PLANETILER_BOUNDS is what keeps the output honest. A Geofabrik area is clipped
# to a polygon, so the "alps" area produced tiles that were served but empty
# outside it — Basel came back as a 0-byte tile. --bounds keeps a true
# rectangle, so there are no holes inside it.
#
# The "europe" source is ~28 GB to download once; only the bounds are processed,
# so the archive stays a few GB. Expect an hour or two on a laptop.
#
#     ./scripts/build-extract.sh
#     PLANETILER_BOUNDS=3,42,19,51 ./scripts/build-extract.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

jar="planetiler.jar"
jar_url="https://github.com/onthegomap/planetiler/releases/download/v${PLANETILER_VERSION}/planetiler.jar"

# Prefer $JAVA_HOME over whatever is on PATH. Homebrew's versioned JDKs are
# keg-only — installed but not symlinked — so on macOS `java` is usually
# Apple's stub, which reports "Unable to locate a Java Runtime" even when a
# working JDK is in the cellar. JAVA_HOME is how you point at it without
# putting a second Java on PATH for everything else.
if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/java" ]; then
    java="${JAVA_HOME}/bin/java"
elif command -v java >/dev/null 2>&1 && java -version >/dev/null 2>&1; then
    java="java"
else
    cat >&2 <<'EOF'
error: no usable Java runtime; planetiler needs Java 21+

macOS with Homebrew:
    brew install openjdk@21
    JAVA_HOME=$(brew --prefix openjdk@21) ./scripts/build-extract.sh

`brew install` alone is not enough — openjdk@21 is keg-only, so nothing is
added to PATH and /usr/bin/java stays Apple's stub.
EOF
    exit 1
fi

version=$("$java" -version 2>&1 | head -1)
echo "==> using ${java} (${version})"

if [ ! -f "$jar" ]; then
    echo "==> downloading planetiler ${PLANETILER_VERSION}"
    curl -fL --progress-bar -o "$jar" "$jar_url"
fi

mkdir -p "$DIST_DIR"

echo "==> building ${PLANETILER_AREA} within ${PLANETILER_BOUNDS}"
echo "    -> ${DIST_DIR}/${PMTILES_NAME}"
"$java" "-Xmx${PLANETILER_MEMORY}" -jar "$jar" \
    --download \
    --area="$PLANETILER_AREA" \
    --bounds="$PLANETILER_BOUNDS" \
    --output="${DIST_DIR}/${PMTILES_NAME}" \
    --force

echo "==> built $(du -h "${DIST_DIR}/${PMTILES_NAME}" | cut -f1) archive"
echo "    inspect it with: pmtiles show ${DIST_DIR}/${PMTILES_NAME}"
