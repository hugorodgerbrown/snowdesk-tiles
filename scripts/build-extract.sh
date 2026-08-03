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

echo "==> building ${PLANETILER_AREA} -> ${DIST_DIR}/${PMTILES_NAME}"
"$java" "-Xmx${PLANETILER_MEMORY}" -jar "$jar" \
    --download \
    --area="$PLANETILER_AREA" \
    --output="${DIST_DIR}/${PMTILES_NAME}" \
    --force

echo "==> built $(du -h "${DIST_DIR}/${PMTILES_NAME}" | cut -f1) archive"
echo "    inspect it with: pmtiles show ${DIST_DIR}/${PMTILES_NAME}"
