#!/usr/bin/env bash
# Assemble the complete dist tree ready for upload (SNOW-485).
#
# Mirrors sprites and glyphs from upstream, then rewrites the style JSON to
# point at our own origin and PMTiles archive. Does NOT build the vector tile
# archive — that is ./scripts/build-extract.sh, which is slow and rarely needs
# rerunning. Both must have run before ./scripts/upload.sh.
#
#     ./scripts/build.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

# PYTHON must be a path to an interpreter, not a command line — it is invoked
# quoted, so "uv run python" would be looked up as a single executable name and
# fail. Check it up front: the mirror is a ~1 GB download and failing 20 minutes
# in over the interpreter is a poor use of anyone's afternoon.
python=${PYTHON:-python3}

if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    cat >&2 <<EOF
error: ${python} is not a usable Python 3.12+ interpreter

PYTHON must be a path to an interpreter, not a command line. With uv:

    PYTHON=\$(uv python find 3.12) ./scripts/build.sh
EOF
    exit 1
fi

echo "==> mirroring sprites and glyphs from ${UPSTREAM_ORIGIN}"
"$python" scripts/mirror_assets.py --dist "$DIST_DIR"

echo "==> rewriting style for ${TILES_ORIGIN} (${PMTILES_NAME})"
mkdir -p "${DIST_DIR}/styles"
# Write to a temp file first: rewrite_style.py exits non-zero when an upstream
# reference survives the rewrite, and a shell redirect would have already
# truncated the real target by then.
"$python" scripts/rewrite_style.py \
    --origin "$TILES_ORIGIN" \
    --tile-path "$TILE_PATH" \
    --min-zoom "$TILE_MIN_ZOOM" \
    --max-zoom "$TILE_MAX_ZOOM" \
    >"${DIST_DIR}/styles/liberty.tmp"
mv "${DIST_DIR}/styles/liberty.tmp" "${DIST_DIR}/styles/liberty"

if [ ! -f "${DIST_DIR}/${PMTILES_NAME}" ]; then
    echo "warning: ${DIST_DIR}/${PMTILES_NAME} is missing." >&2
    echo "         Run ./scripts/build-extract.sh before uploading." >&2
fi

echo "==> dist tree ready:"
du -sh "$DIST_DIR"/* 2>/dev/null || true
