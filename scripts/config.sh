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
: "${PMTILES_NAME:=alps.pmtiles}"

# XYZ template the Worker serves, relative to the origin. Written into the style
# as the vector source's tiles array; must match the route in
# worker/wrangler.toml.
# Not the `: "${VAR:=default}"` form the other settings use: the value contains
# braces, and inside a parameter expansion the first unescaped `}` ends the
# expansion — `${TILE_PATH:=tiles/{z}/...}` yields "tiles/{z". Escaping the
# braces does not help either; the backslashes survive into the value and get
# published in the style. A plain single-quoted assignment is the only form that
# round-trips.
if [ -z "${TILE_PATH:-}" ]; then
    TILE_PATH='tiles/{z}/{x}/{y}.mvt'
fi

# Geofabrik area planetiler builds. "alps" covers CH / AT / IT-South-Tyrol /
# FR-Alps — every current avalanche region.
: "${PLANETILER_AREA:=alps}"
: "${PLANETILER_VERSION:=0.8.3}"
: "${PLANETILER_MEMORY:=8g}"

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
export IMMUTABLE_CACHE STYLE_CACHE TILE_PATH
