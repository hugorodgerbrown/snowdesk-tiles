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

# --- Terrain elevation grid (SNOW-908) --------------------------------------
#
# The terrain tileset is a second, independent set of objects on the same
# origin: Int16 heights on a 5 m grid in EPSG:3035, sampled per point by Django
# (SNOW-917) rather than rendered by MapLibre.
#
# Nothing about the grid's *geometry* is here on purpose. Cell size, tile size,
# skirt width, projection and the height encoding live in
# scripts/terrain_grid.py and are published in grid.json, because they are a
# contract with another repository rather than a knob on this build. What is
# settable here is which ground to cover and where to do the work.

# Version segment in the terrain tile URLs. Same job as TILE_VERSION: tiles are
# served immutable for a year and cached by URL, so a rebuilt grid under a fixed
# path would be invisible to every client still holding the old one. The Worker
# ignores the segment and always reads terrain/{x}/{y}.s16, so a bump needs no
# Worker deploy — only a rebuilt grid.json naming the new prefix.
#
# Bump it for any change to the grid definition, not just to the heights: a
# client caching tiles cut on one geometry and reading them under another
# decodes silently wrong answers rather than failing.
: "${TERRAIN_VERSION:=v1}"

# XYZ-style template the Worker serves, relative to the origin. Written into
# grid.json as tile_url_template.
#
# Assigned rather than defaulted with `${VAR:=default}` for the same reason as
# TILE_PATH: the value contains braces, and inside a parameter expansion the
# first unescaped `}` ends the expansion.
if [ -z "${TERRAIN_TILE_PATH:-}" ]; then
    TERRAIN_TILE_PATH="terrain/${TERRAIN_VERSION}/{x}/{y}.s16"
fi

# Subdirectory of DIST_DIR, and the bucket prefix. The Worker maps
# /terrain/<version>/{x}/{y}.s16 onto terrain/{x}/{y}.s16 in the bucket.
: "${TERRAIN_DIR:=terrain}"

# Ground to cover, as a WGS84 bounding box: west south east north, space
# separated. The default is swissALTI3D's own published extent — Switzerland
# and Liechtenstein — so the fetch asks for everything the source has.
#
# The *grid* is Alps-wide regardless; this is only which part of it gets data.
# Narrow it to build one region while iterating:
#
#     TERRAIN_BBOX="7.5 46.0 8.0 46.4" ./scripts/build-terrain.sh
: "${TERRAIN_BBOX:=5.95 45.72 10.50 47.83}"

# Source resolution to download, in metres. swissALTI3D publishes 0.5 m and 2 m;
# 2 m still oversamples the 5 m grid enough for the box filter to have something
# to average, at a sixteenth of the download.
: "${TERRAIN_GSD:=2}"

# Where the build's intermediates live: the downloaded GeoTIFFs, the warped
# raster and the flat grid. Tens of GB, all of it disposable once the tiles are
# published. Gitignored.
: "${TERRAIN_WORK_DIR:=work/terrain}"

# Parallel downloads. The source is tens of thousands of small files, so this is
# latency-bound rather than bandwidth-bound; 8 is polite to data.geo.admin.ch
# and still saturates a normal link.
: "${TERRAIN_JOBS:=8}"

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
export TERRAIN_VERSION TERRAIN_TILE_PATH TERRAIN_DIR TERRAIN_BBOX TERRAIN_GSD
export TERRAIN_WORK_DIR TERRAIN_JOBS
