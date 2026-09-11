#!/usr/bin/env python3
r"""The terrain elevation grid: its geometry, its encoding, and its contract.

SNOW-908. This module is the single definition of the grid that the terrain
tiles are cut on. It is also the **contract with SNOW-917** — the Django side
samples these tiles, and every number it needs to turn a coordinate into a
height lives here and is published as ``grid.json`` alongside the tiles.

The shape of the decision, because none of it is recoverable from the numbers:

**Heights are stored, not slope.** Slope is a derivative over a neighbourhood,
so the *analysis window* — how far apart the two heights you difference are —
decides which terrain features survive. A 90 m window averages away the 40 m
steep step that catches people; a 6 m window measures boulders and gully walls.
Storing heights keeps the window a read-time parameter: a 3x3 neighbourhood on
this grid is a 15 m window, 5x5 is 25 m, box-filter to 10 m first and you have a
30 m window, all from the same bytes. Storing precomputed slope would foreclose
every window but one, and SNOW-911's crux analysis needs curvature and
neighbourhood statistics that no slope raster retains.

**The stored grid is 5 m; the default analysis window is 10 m.** They are
deliberately different numbers. 5 m is chosen because terrain is static, so the
build is one-off and storage is the only ongoing cost — a few GB in R2, which is
fractions of a cent a month. Storing coarser would permanently foreclose every
window below the storage spacing to save money we are not spending. The 10 m
default matches what swisstopo compute ``ch.swisstopo.hangneigung-ueber_30`` at,
which is the raster SNOW-910's route colouring has to agree with: a finer line
over a 10 m raster puts two different answers on one screen.

**EPSG:3035, not swissALTI3D's native EPSG:2056.** One reprojection at build
time now, against rebuilding the whole grid the first time a source outside
Switzerland is added. The grid is Alps-wide from day one and only the Swiss part
has data in it; adding Copernicus GLO-30 later (SNOW-693) is "resample a coarser
source onto the existing grid", and the grid never moves.

**Int16 at a 0.25 m scale, not Int16 metres.** Two bytes either way, and the
scale is not decoration. Rounding heights to whole metres puts +/-0.5 m of
independent noise on every cell; differenced across a 10 m window that is +/-1 m
of error on the rise, which at a true 35 degrees spans 31.0 to 38.7 degrees. The
thresholds this data exists to resolve — 30, 35, 40 — are 5 degrees apart, so
metre quantisation would be larger than the distinction being drawn. At 0.25 m
the same worst case is +/-1.0 degree, and the quantisation step sits below
swissALTI3D's own stated vertical accuracy (0.3-0.5 m from LiDAR below 2000 m,
1-3 m from stereo correlation above it) rather than above it.

Standalone operational tooling: stdlib only, no GDAL and no numpy. The heavy
lifting — reproject and resample — is done by the GDAL command line in
``build-terrain.sh``; what lives here is the arithmetic that both the tile
cutter and the acceptance checks have to agree on, and that SNOW-917 has to
reimplement. Keeping it dependency-free is what lets it be tested, and read, on
its own.

Usage:
    python scripts/terrain_grid.py definition --origin https://host --version v1
    python scripts/terrain_grid.py extent --manifest work/manifest.json
    python scripts/terrain_grid.py cell 7.748056 46.020833
    curl -s .../terrain/v1/3238/2005.s16 \
        | python scripts/terrain_grid.py read-cell 44 112
    python scripts/terrain_grid.py gdal-args
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any, NamedTuple

# --- The contract -----------------------------------------------------------
#
# Every value below is published in grid.json and consumed by SNOW-917. Changing
# any of them invalidates every published tile: bump TERRAIN_VERSION in
# config.sh in the same commit, or clients keep being served the old bytes under
# the new rules (tiles are cached immutable for a year, by URL).

GRID_ID = "snowdesk-terrain-5m-3035"

#: ETRS89-LAEA. Equal-area and metric across the whole Alps, so a cell is the
#: same size in Chamonix as in the Zillertal and a slope is a slope everywhere.
GRID_CRS = "EPSG:3035"

#: Cell spacing in metres. The *storage* grid — see the module docstring for why
#: this is not the same number as the analysis window.
CELL_SIZE_M = 5

#: Data cells along each side of a tile, excluding the skirt.
TILE_CELLS = 256

#: Cells of overlap on each of a tile's four sides. One is the minimum that lets
#: a sample landing in the outermost data cell read its own neighbours, which is
#: what a 3x3 slope operator needs. Without it every tile boundary would be a
#: seam of wrong or missing answers 1280 m apart.
SKIRT_CELLS = 1

#: Stored heights are ``round(metres / HEIGHT_SCALE_M)`` as little-endian Int16.
HEIGHT_SCALE_M = 0.25

#: No data here. Distinct from every representable height, including 0 m — a
#: caller must never be able to mistake "outside coverage" for "flat ground".
NODATA = -32768
STORED_MAX = 32767

DTYPE = "int16"
BYTE_ORDER = "little"

#: Row 0 of a tile is its northernmost, column 0 its westernmost — a plain
#: north-up raster, as GDAL writes one.
ROW_ORDER = "north-to-south"
COLUMN_ORDER = "west-to-east"

#: Which cell owns a coordinate that lands exactly on a boundary. Published
#: because it is not GDAL's rule and SNOW-917 has to match it — see ``cell_for``.
CELL_BOUNDARY_RULE = (
    "half-open [south, north) and [west, east): a coordinate on a cell or tile "
    "boundary belongs to the cell north and east of it"
)

#: Tile indices count east and north from the CRS origin, so tile (x, y) covers
#: eastings [x * TILE_SIZE_M, (x+1) * TILE_SIZE_M) and northings
#: [y * TILE_SIZE_M, (y+1) * TILE_SIZE_M). Both are positive everywhere in
#: Europe under EPSG:3035, which is why the Worker's route can match \\d+ and
#: reject anything else.
GRID_ORIGIN_EASTING = 0
GRID_ORIGIN_NORTHING = 0

#: The window SNOW-917 samples at unless a caller asks for another. Not a
#: property of the data — see the module docstring.
DEFAULT_ANALYSIS_WINDOW_M = 10

# --- Derived, never edited independently ------------------------------------

TILE_SIZE_M = TILE_CELLS * CELL_SIZE_M
STORED_CELLS = TILE_CELLS + 2 * SKIRT_CELLS
TILE_BYTES = STORED_CELLS * STORED_CELLS * 2

#: The float sentinel that quantises exactly onto NODATA, and so is what
#: ``gdalwarp -dstnodata`` must be given.
HEIGHT_NODATA_M = NODATA * HEIGHT_SCALE_M
HEIGHT_MIN_M = (NODATA + 1) * HEIGHT_SCALE_M
HEIGHT_MAX_M = STORED_MAX * HEIGHT_SCALE_M

# --- The source registry ----------------------------------------------------
#
# One entry today. Native resolution is recorded separately from cell spacing
# and travels with every sampled height, because they are not the same claim:
# resampling GLO-30 onto a 5 m grid is honest only so long as nothing downstream
# reads 5 m cells as 5 m of information. SNOW-917's registry mirrors this.

SOURCES: dict[str, dict[str, Any]] = {
    "swissalti3d": {
        "id": "swissalti3d",
        "name": "swissALTI3D",
        "provider": "Federal Office of Topography swisstopo",
        "native_resolution_m": 2.0,
        "quality": "lidar",
        # swisstopo's own figures: 0.3 m from the current LiDAR generation,
        # 0.5 m from the previous one below 2000 m, and 1-3 m from stereo
        # correlation above 2000 m — which is most of what a ski tourer is
        # standing on. The 0.25 m storage step is below all of them.
        "vertical_accuracy_m": {"lidar": 0.5, "stereo_above_2000m": 3.0},
        "surface": "terrain",
        # Free geodata (OGD) since 1 March 2021: may be used, distributed, made
        # accessible, enriched, processed and used commercially, with the source
        # indicated. geocat records it as "Opendata BY: Open use. Must provide
        # the source." Creative Commons licences are deliberately not used —
        # swisstopo state they are incompatible with GeoIG/GeoIV.
        "attribution": "© swisstopo",
        "licence": "swisstopo free geodata (OGD)",
        "licence_url": (
            "https://www.swisstopo.admin.ch/en/"
            "terms-of-use-free-geodata-and-geoservices"
        ),
        "source_url": "https://www.swisstopo.admin.ch/en/height-model-swissalti3d",
        "stac_collection": "ch.swisstopo.swissalti3d",
    },
}

#: Shown wherever a sampled height or a slope derived from it is surfaced.
#: swisstopo accept "Source: Federal Office of Topography swisstopo" or
#: "(c) swisstopo"; the short form is what goes on a map legend.
ATTRIBUTION = "© swisstopo"


class Cell(NamedTuple):
    """A single grid cell, addressed as a tile plus a position within it."""

    tile_x: int
    tile_y: int
    row: int
    col: int


class Extent(NamedTuple):
    """A tile-aligned rectangle, in projected metres and in tile indices."""

    west: int
    south: int
    east: int
    north: int
    tile_x: int
    tile_y: int
    tiles_x: int
    tiles_y: int


# --- Geometry ---------------------------------------------------------------


def tile_for(easting: float, northing: float) -> tuple[int, int]:
    """Return the (x, y) tile index holding a projected coordinate."""
    return (
        math.floor(easting / TILE_SIZE_M),
        math.floor(northing / TILE_SIZE_M),
    )


def tile_bounds(tile_x: int, tile_y: int) -> tuple[int, int, int, int]:
    """Return (west, south, east, north) of a tile's data area, in metres."""
    west = tile_x * TILE_SIZE_M
    south = tile_y * TILE_SIZE_M
    return west, south, west + TILE_SIZE_M, south + TILE_SIZE_M


def stored_bounds(tile_x: int, tile_y: int) -> tuple[int, int, int, int]:
    """Return the bounds of what a tile *file* covers — its data plus skirt."""
    west, south, east, north = tile_bounds(tile_x, tile_y)
    skirt = SKIRT_CELLS * CELL_SIZE_M
    return west - skirt, south - skirt, east + skirt, north + skirt


def cell_for(easting: float, northing: float) -> Cell:
    """Return the tile and the (row, col) within it holding a coordinate.

    Row and column index the *stored* array, so the first data cell is
    ``(SKIRT_CELLS, SKIRT_CELLS)`` and the skirt occupies the outer ring.

    Cells are half-open ``[south, north)`` and ``[west, east)``, so a coordinate
    landing exactly on a boundary belongs to the cell north and east of it, and
    never to two cells at once. That is the same rule ``tile_for`` applies, and
    it has to be: ``tile_for`` puts a coordinate on a tile's south edge inside
    that tile, so the southernmost row must own its own south edge or the two
    would disagree along every tile boundary in the grid.

    It differs from GDAL's pixel convention, which resolves a boundary northing
    downwards instead. The difference only appears on exact multiples of the
    cell size and moves the answer by one cell, but SNOW-917 has to apply this
    rule rather than GDAL's to read the cell this grid says it is reading —
    which is why it is published in ``grid.json`` rather than left implied.
    """
    tile_x, tile_y = tile_for(easting, northing)
    col = math.floor((easting - tile_x * TILE_SIZE_M) / CELL_SIZE_M)
    # Rows run the other way to northings, so the northernmost data row is 0.
    row = TILE_CELLS - 1 - math.floor((northing - tile_y * TILE_SIZE_M) / CELL_SIZE_M)
    return Cell(tile_x, tile_y, row + SKIRT_CELLS, col + SKIRT_CELLS)


def cell_centre(tile_x: int, tile_y: int, row: int, col: int) -> tuple[float, float]:
    """Return the projected coordinate at the centre of a stored cell."""
    west, _, _, north = tile_bounds(tile_x, tile_y)
    return (
        west + (col - SKIRT_CELLS + 0.5) * CELL_SIZE_M,
        north - (row - SKIRT_CELLS + 0.5) * CELL_SIZE_M,
    )


def snap_extent(
    west: float,
    south: float,
    east: float,
    north: float,
    margin_tiles: int = 1,
) -> Extent:
    """Snap a projected bounding box out to whole tiles, with a margin.

    The margin exists so that tiles holding data at the edge of coverage still
    have their skirt read from real ground rather than from the end of the
    warped raster. One tile is enough — the skirt is one cell — and the extra
    ring costs nothing, because the cutter writes no file for a tile with no
    data in it.
    """
    tile_x = math.floor(west / TILE_SIZE_M) - margin_tiles
    tile_y = math.floor(south / TILE_SIZE_M) - margin_tiles
    last_x = math.ceil(east / TILE_SIZE_M) - 1 + margin_tiles
    last_y = math.ceil(north / TILE_SIZE_M) - 1 + margin_tiles
    return Extent(
        west=tile_x * TILE_SIZE_M,
        south=tile_y * TILE_SIZE_M,
        east=(last_x + 1) * TILE_SIZE_M,
        north=(last_y + 1) * TILE_SIZE_M,
        tile_x=tile_x,
        tile_y=tile_y,
        tiles_x=last_x - tile_x + 1,
        tiles_y=last_y - tile_y + 1,
    )


# --- Encoding ---------------------------------------------------------------


def encode_height(metres: float) -> int:
    """Quantise a height in metres onto the stored Int16 scale."""
    if not HEIGHT_MIN_M <= metres <= HEIGHT_MAX_M:
        raise ValueError(
            f"height {metres} m is outside the representable range "
            f"[{HEIGHT_MIN_M}, {HEIGHT_MAX_M}]"
        )
    return round(metres / HEIGHT_SCALE_M)


def decode_height(stored: int) -> float | None:
    """Return the height in metres, or ``None`` where the grid has no data.

    ``None`` rather than any number at all: the one failure this whole design is
    arranged to prevent is an absent height reading downstream as flat ground.
    """
    if stored == NODATA:
        return None
    return stored * HEIGHT_SCALE_M


def read_cell(tile: bytes, row: int, col: int) -> float | None:
    """Decode one cell out of a tile body, or ``None`` where there is no data."""
    if len(tile) != TILE_BYTES:
        raise ValueError(f"tile is {len(tile)} bytes, expected {TILE_BYTES}")
    if not (0 <= row < STORED_CELLS and 0 <= col < STORED_CELLS):
        raise ValueError(f"cell ({row}, {col}) is outside the stored {STORED_CELLS}^2")
    offset = (row * STORED_CELLS + col) * 2
    (stored,) = struct.unpack_from("<h", tile, offset)
    return decode_height(stored)


# --- Projection -------------------------------------------------------------
#
# Lambert Azimuthal Equal Area, forward only, on GRS80 — the EPSG guidance note
# formulas. Roughly thirty lines of stdlib arithmetic against a PROJ dependency
# that this repo would otherwise not have at all: pyproj is a binary wheel, and
# the only thing that needs it is turning a handful of lon/lat probes into grid
# coordinates so the acceptance checks can name real places. Agreement with
# PROJ was measured at better than 0.1 mm across Switzerland; the test suite
# pins that against fixed control points.


def _authalic_q(lat: float, e: float, e2: float) -> float:
    """Return the authalic-latitude integral q for a geodetic latitude."""
    sin_lat = math.sin(lat)
    return (1 - e2) * (
        sin_lat / (1 - e2 * sin_lat * sin_lat)
        - (1 / (2 * e)) * math.log((1 - e * sin_lat) / (1 + e * sin_lat))
    )


def lonlat_to_grid(lon: float, lat: float) -> tuple[float, float]:
    """Project WGS84 degrees to EPSG:3035 easting/northing in metres."""
    # GRS80, and the EPSG:3035 origin and false origin.
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    lat_0, lon_0 = math.radians(52.0), math.radians(10.0)
    false_easting, false_northing = 4321000.0, 3210000.0

    q_p = _authalic_q(math.pi / 2, e, e2)
    q_0 = _authalic_q(lat_0, e, e2)
    r_q = a * math.sqrt(q_p / 2)
    beta_0 = math.asin(q_0 / q_p)
    d = (
        a
        * (math.cos(lat_0) / math.sqrt(1 - e2 * math.sin(lat_0) ** 2))
        / (r_q * math.cos(beta_0))
    )

    lam, phi = math.radians(lon), math.radians(lat)
    beta = math.asin(_authalic_q(phi, e, e2) / q_p)
    b = r_q * math.sqrt(
        2
        / (
            1
            + math.sin(beta_0) * math.sin(beta)
            + math.cos(beta_0) * math.cos(beta) * math.cos(lam - lon_0)
        )
    )
    easting = false_easting + (b * d) * (math.cos(beta) * math.sin(lam - lon_0))
    northing = false_northing + (b / d) * (
        math.cos(beta_0) * math.sin(beta)
        - math.sin(beta_0) * math.cos(beta) * math.cos(lam - lon_0)
    )
    return easting, northing


def lonlat_extent(
    west: float,
    south: float,
    east: float,
    north: float,
    steps: int = 40,
) -> tuple[float, float, float, float]:
    """Return the projected bounding box of a WGS84 box, in EPSG:3035 metres.

    The edges are densified rather than the four corners projected: LAEA bends
    straight lines, so a box's projected extent is wider than its projected
    corners and cutting to the corners would clip coverage off the sides.
    """
    eastings, northings = [], []
    for i in range(steps + 1):
        lon = west + (east - west) * i / steps
        lat = south + (north - south) * i / steps
        for point in (
            lonlat_to_grid(lon, south),
            lonlat_to_grid(lon, north),
            lonlat_to_grid(west, lat),
            lonlat_to_grid(east, lat),
        ):
            eastings.append(point[0])
            northings.append(point[1])
    return min(eastings), min(northings), max(eastings), max(northings)


# --- The published definition -----------------------------------------------


def definition(origin: str = "", version: str = "") -> dict[str, Any]:
    """Return the grid definition as published in ``grid.json``.

    ``origin`` and ``version`` are what turn the abstract grid into fetchable
    URLs; omit them and the geometry is still fully described, which is what the
    acceptance checks compare against.
    """
    doc: dict[str, Any] = {
        "grid": GRID_ID,
        "crs": GRID_CRS,
        "cell_size_m": CELL_SIZE_M,
        "tile_cells": TILE_CELLS,
        "tile_size_m": TILE_SIZE_M,
        "skirt_cells": SKIRT_CELLS,
        "stored_cells": STORED_CELLS,
        "tile_bytes": TILE_BYTES,
        "dtype": DTYPE,
        "byte_order": BYTE_ORDER,
        "height_scale_m": HEIGHT_SCALE_M,
        "height_offset_m": 0,
        "nodata": NODATA,
        "row_order": ROW_ORDER,
        "column_order": COLUMN_ORDER,
        "cell_boundary_rule": CELL_BOUNDARY_RULE,
        "origin": {
            "easting": GRID_ORIGIN_EASTING,
            "northing": GRID_ORIGIN_NORTHING,
        },
        "default_analysis_window_m": DEFAULT_ANALYSIS_WINDOW_M,
        "attribution": ATTRIBUTION,
        # Said explicitly because the alternative reading is the dangerous one:
        # a tile that is absent is not an error and not flat ground.
        "absent_tile": (
            "204 No Content means no source covers this tile. It is not an "
            "error and must never be read as level terrain."
        ),
    }
    if version:
        doc["version"] = version
    if origin and version:
        doc["tile_url_template"] = (
            f"{origin.rstrip('/')}/terrain/{version}/{{x}}/{{y}}.s16"
        )
    return doc


# --- CLI --------------------------------------------------------------------


def _cmd_definition(args: argparse.Namespace) -> int:
    json.dump(definition(args.origin, args.version), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_extent(args: argparse.Namespace) -> int:
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        box = tuple(manifest["bbox"])
    else:
        box = tuple(args.bbox)
    extent = snap_extent(*lonlat_extent(*box), margin_tiles=args.margin_tiles)
    print(
        extent.west,
        extent.south,
        extent.east,
        extent.north,
        extent.tile_x,
        extent.tile_y,
        extent.tiles_x,
        extent.tiles_y,
    )
    return 0


def _cmd_cell(args: argparse.Namespace) -> int:
    cell = cell_for(*lonlat_to_grid(args.lon, args.lat))
    print(cell.tile_x, cell.tile_y, cell.row, cell.col)
    return 0


def _cmd_read_cell(args: argparse.Namespace) -> int:
    body = sys.stdin.buffer.read()
    # Distinguishable from a height and from "nodata": a truncated body is a
    # broken publish, not an answer about the terrain.
    if len(body) != TILE_BYTES:
        print("short")
        return 1
    height = read_cell(body, args.row, args.col)
    print("nodata" if height is None else f"{height:.2f}")
    return 0


def _cmd_gdal_args(args: argparse.Namespace) -> int:
    # Emitted rather than written into build-terrain.sh so the encoding cannot
    # drift from the encoding this module documents and the tests pin.
    if args.what == "dstnodata":
        print(HEIGHT_NODATA_M)
    elif args.what == "scale":
        # gdal_translate -scale src_min src_max dst_min dst_max: an exact linear
        # map of metres onto the stored scale, with the float nodata sentinel
        # landing on NODATA at the bottom of it.
        print(HEIGHT_NODATA_M, HEIGHT_MAX_M, NODATA, STORED_MAX)
    else:
        print(CELL_SIZE_M)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — dispatch to one of the grid queries."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_def = sub.add_parser("definition", help="emit grid.json")
    p_def.add_argument("--origin", default="", help="public origin serving the tiles")
    p_def.add_argument("--version", default="", help="tile URL version segment")
    p_def.set_defaults(func=_cmd_definition)

    p_ext = sub.add_parser("extent", help="snap a WGS84 box to whole tiles")
    p_ext.add_argument("--manifest", help="fetch manifest to read the bbox from")
    p_ext.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("W", "S", "E", "N"),
        help="WGS84 bounding box, if no manifest",
    )
    p_ext.add_argument("--margin-tiles", type=int, default=1)
    p_ext.set_defaults(func=_cmd_extent)

    p_cell = sub.add_parser("cell", help="lon/lat to tile x, y and row, col")
    p_cell.add_argument("lon", type=float)
    p_cell.add_argument("lat", type=float)
    p_cell.set_defaults(func=_cmd_cell)

    p_read = sub.add_parser("read-cell", help="decode one cell of a tile on stdin")
    p_read.add_argument("row", type=int)
    p_read.add_argument("col", type=int)
    p_read.set_defaults(func=_cmd_read_cell)

    p_gdal = sub.add_parser("gdal-args", help="values the GDAL steps need")
    p_gdal.add_argument("what", choices=["dstnodata", "scale", "cell-size"])
    p_gdal.set_defaults(func=_cmd_gdal_args)

    args = parser.parse_args(argv)
    if args.command == "extent" and not (args.manifest or args.bbox):
        parser.error("extent needs --manifest or --bbox")
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
