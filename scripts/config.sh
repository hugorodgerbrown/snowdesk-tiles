#!/usr/bin/env bash
# Shared configuration for the tile build/publish scripts (SNOW-485).
#
# Every value is an environment variable with a default, so nothing about the
# deployment is baked into the script bodies. Override any of them in the
# calling shell:
#
#     R2_BUCKET=snowdesk-tiles-staging ./scripts/upload.sh
#
# Sourced by the other scripts; not meant to be run directly.

# The public origin the assets will be served from. Also the origin written
# into the rewritten style JSON.
#
# Deliberately a separate registrable domain from the site: cookies scoped to
# snowdesk.info can never ride along on the hundreds of Range requests a map
# session fires at the archive.
: "${TILES_ORIGIN:=https://tiles.snowdesk-data.info}"

# R2 bucket holding the published assets.
: "${R2_BUCKET:=snowdesk-tiles}"

# Filename of the vector-tile archive, on disk and as the R2 object key. The
# Worker reads it through its R2 binding — keep PMTILES_KEY in worker/wrangler.toml
# in step with this.
: "${PMTILES_NAME:=snowdesk.pmtiles}"

# Tile URL version. Bump this whenever the archive is rebuilt.
#
# Tiles are served immutable for a year and cached by URL, at the edge and in the
# browser. Replacing the archive under a fixed path therefore changes nothing a
# client can see: the old tiles keep being served until they expire. Putting a
# version in the path gives the new archive new URLs, so the swap takes effect
# at once and no cache purge is needed.
#
# The Worker ignores the version segment and always reads PMTILES_KEY, so a bump
# needs only the style rebuilt and re-uploaded — not a Worker deploy. Requests
# for the previous version keep working and return current data, which matters
# while the old style is still within its one-hour TTL.
: "${TILE_VERSION:=v1}"

# XYZ template the Worker serves, relative to the origin. Written into the style
# as the vector source's tiles array.
#
# Not the `: "${VAR:=default}"` form the other settings use: the value contains
# braces, and inside a parameter expansion the first unescaped `}` ends the
# expansion — `${TILE_PATH:=tiles/{z}/...}` yields "tiles/{z". Escaping the
# braces does not help either; the backslashes survive into the value and get
# published in the style. A plain single-quoted assignment is the only form that
# round-trips.
if [ -z "${TILE_PATH:-}" ]; then
    TILE_PATH="tiles/${TILE_VERSION}/{z}/{x}/{y}.mvt"
fi

# Zoom range the archive holds, written into the style's vector source.
#
# Not cosmetic. Upstream carries this in the TileJSON its `url` points at, and
# the rewrite drops `url` in favour of a `tiles` template — so if the style does
# not state the range, MapLibre assumes maxzoom 22, asks for z15+ tiles that do
# not exist, and renders nothing above z14 instead of overzooming.
#
# Keep in step with the archive: build-extract.sh takes planetiler's default
# maximum of 14. The live values are readable from the Worker's TileJSON, which
# derives them from the PMTiles header:
#
#     curl -s "${TILES_ORIGIN}/tiles/${TILE_VERSION}/tiles.json"
: "${TILE_MIN_ZOOM:=0}"
: "${TILE_MAX_ZOOM:=14}"

# Upstream OpenFreeMap origin the assets are mirrored from.
: "${UPSTREAM_ORIGIN:=https://tiles.openfreemap.org}"
: "${UPSTREAM_STYLE_URL:=${UPSTREAM_ORIGIN}/styles/liberty}"

# Local staging tree. Mirrors the object layout of the bucket exactly.
: "${DIST_DIR:=dist}"

# Cache-Control values. Tiles, glyphs and sprites are content-addressed by
# snapshot and never change in place. The style is the mutable pointer — it
# names the current PMTiles file — so it gets a short TTL.
: "${IMMUTABLE_CACHE:=public, max-age=31536000, immutable}"
: "${STYLE_CACHE:=public, max-age=3600}"

export TILES_ORIGIN R2_BUCKET PMTILES_NAME PLANETILER_AREA PLANETILER_VERSION
export PLANETILER_MEMORY UPSTREAM_ORIGIN UPSTREAM_STYLE_URL DIST_DIR
export IMMUTABLE_CACHE STYLE_CACHE TILE_PATH TILE_VERSION PLANETILER_BOUNDS
export TILE_MIN_ZOOM TILE_MAX_ZOOM
