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
    python scripts/terrain_grid.py profile-points
    python scripts/terrain_grid.py profile "Theodul Pass" \
        --url https://tiles.snowdesk-data.info/terrain/v2 \
        --expect dist/terrain/verify-gap.json
    python scripts/terrain_grid.py places --url dist/terrain \
        --expect dist/terrain/verify-gap.json

``profile`` and ``places`` take a base URL or a local tile tree: the build runs
them on its own tree before it uploads, and verify.sh on the origin after.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

# --- The contract -----------------------------------------------------------
#
# Every value below is published in grid.json and consumed by SNOW-917. Changing
# any of them invalidates every published tile: bump TERRAIN_VERSION in
# config.sh in the same commit, or clients keep being served the old bytes under
# the new rules (tiles are cached immutable for a year, by URL).

GRID_ID = "snowdesk-terrain-5m-3035"

#: Named places verify.sh checks the published grid against (SNOW-1505).
VERIFY_POINTS = Path(__file__).resolve().parent / "verify_points.json"

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
    # SNOW-1505: the rest of the Alps, from the national models in our copy of
    # Mapterhorn's tiles, clipped to their own coverage so that ground only
    # Copernicus GLO-30 covers stays absent. One entry for the mosaic, with a
    # credit per model nested under it (cut_terrain_tiles.grid_json adds them,
    # and the release, per build from Mapterhorn's attribution.json).
    "mapterhorn": {
        "id": "mapterhorn",
        "name": "Mapterhorn national models",
        "provider": "Mapterhorn",
        # Not a constant: the coarsest credited model's native resolution, set
        # per build (Trentino's 5 m today). grid_json refuses a figure at or
        # below swissALTI3D's, because SNOW-917 breaks a tie inside a quality
        # tier on resolution, and inside Switzerland swissALTI3D must win.
        "native_resolution_m": None,
        # Every model kept is airborne laser scanning; GLO-30, the one radar
        # source, is clipped out on ingest.
        "quality": "lidar",
        "surface": "terrain",
        "attribution": "© Mapterhorn",
        "licence": "each national model's own open licence; see credits",
        "licence_url": "https://mapterhorn.com/attribution/",
        "source_url": "https://mapterhorn.com",
    },
}

#: Shown wherever a sampled height or a slope derived from it is surfaced.
#: swisstopo accept "Source: Federal Office of Topography swisstopo" or
#: "(c) swisstopo"; the short form is what goes on a map legend. Mapterhorn's
#: per-model credits travel in grid.json's ``credits[]``; this is the legend.
ATTRIBUTION = "© swisstopo, Mapterhorn"

#: The attribution of a build that took swissALTI3D alone, as v1 did.
SWISS_ONLY_ATTRIBUTION = "© swisstopo"


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


# --- Seam profiles (SNOW-1505) ----------------------------------------------
#
# verify.sh samples a line across the border wherever swissALTI3D meets a
# national model, and asks two things of it: that every cell a source covers
# has a height, and that the two sources agree where they meet. "Agree" is a
# seam step: each side's 50 m nearest the seam is fitted with a straight line,
# both lines are projected to the seam, and the step is the difference. A
# straight fit rather than the two nearest cells, because a slope that carries
# on across the border would otherwise read as a step one cell's rise high.

#: Profile defaults: a 2 km line, sampled every 5 m — the cell size. The two
#: listed profiles override the length with 10 km (5 km each side of the
#: border): swissALTI3D runs past the national border, and on 19 Nov 2026
#: it covered the whole of a 2 km line at both Theodul Pass and Piz Buin.
#: The datum check found it stopping up to 1.45 km past a crossing.
PROFILE_LENGTH_M = 2000.0
PROFILE_STEP_M = 5.0
SEAM_FIT_M = 50.0

#: Samples within this distance of the seam are not held to either side's
#: expectation: the seam is located to a sample, not to the metre.
SEAM_SLACK_M = 5.0


def profile_points(
    lon: float,
    lat: float,
    bearing_deg: float,
    length_m: float = PROFILE_LENGTH_M,
    step_m: float = PROFILE_STEP_M,
) -> list[tuple[float, float, float]]:
    """Return (distance, lon, lat) samples along a line centred on a point.

    Distances run from ``-length_m / 2`` to ``+length_m / 2`` along the bearing
    (degrees clockwise from north), so a bearing pointing out of Switzerland
    puts the Swiss side at negative distances. Offsets use the GRS80 radii of
    curvature at the centre — over a kilometre the error is millimetres.
    """
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    phi = math.radians(lat)
    w = math.sqrt(1 - e2 * math.sin(phi) ** 2)
    meridian = a * (1 - e2) / w**3
    normal = a / w
    bearing = math.radians(bearing_deg)
    count = round(length_m / step_m)
    samples = []
    for index in range(count + 1):
        distance = -length_m / 2 + index * step_m
        north = distance * math.cos(bearing)
        east = distance * math.sin(bearing)
        samples.append(
            (
                distance,
                lon + math.degrees(east / (normal * math.cos(phi))),
                lat + math.degrees(north / meridian),
            )
        )
    return samples


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Return the least-squares (slope, intercept) of ys on xs."""
    if len(xs) < 2:
        raise ValueError("a line needs at least two points")
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("every point is at the same distance")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope, mean_y - slope * mean_x


def seam_step(
    distances: list[float],
    heights: list[float | None],
    seam_m: float,
    fit_m: float = SEAM_FIT_M,
) -> float:
    """Return the step between the heights the two sides predict at the seam.

    The near side is ``[seam - fit_m, seam)``, the far side
    ``(seam, seam + fit_m]``; samples without a height are left out, and a side
    with fewer than two heights is an error rather than a step of zero.
    """
    sides: list[tuple[list[float], list[float]]] = [([], []), ([], [])]
    for distance, height in zip(distances, heights, strict=True):
        if height is None:
            continue
        if seam_m - fit_m <= distance < seam_m:
            sides[0][0].append(distance)
            sides[0][1].append(height)
        elif seam_m < distance <= seam_m + fit_m:
            sides[1][0].append(distance)
            sides[1][1].append(height)
    predictions = []
    for label, (xs, ys) in zip(("near", "far"), sides, strict=True):
        if len(xs) < 2:
            raise ValueError(
                f"the {label} side has {len(xs)} heights within {fit_m:g} m of "
                "the seam; a step needs a fit on both sides"
            )
        slope, intercept = linear_fit(xs, ys)
        predictions.append(slope * seam_m + intercept)
    return abs(predictions[0] - predictions[1])


def assess_profile(
    distances: list[float],
    heights: list[float | None],
    expect: dict[str, Any],
    max_step_m: float,
) -> tuple[list[str], float | None]:
    """Return a profile's failures, and its seam step where one was measured.

    ``expect`` is the profile's entry in verify-gap.json: where the seam is,
    which stretches a national model covers, and whether the foreign side is
    covered at all. Swiss-side samples must all have heights; covered foreign
    samples must too; an uncovered foreign side must have none — the 204 that
    says no national model is there. The step is measured only where both
    sides are covered.
    """
    seam = float(expect["seam_m"])
    covered = expect.get("covered", [])
    foreign_covered = bool(expect.get("foreign_covered"))
    failures = []
    for distance, height in zip(distances, heights, strict=True):
        if distance < seam - SEAM_SLACK_M and height is None:
            failures.append(f"no height on the Swiss side at {distance:g} m")
        elif distance > seam + SEAM_SLACK_M:
            inside = any(low <= distance <= high for low, high in covered)
            if inside and height is None:
                failures.append(f"no height inside coverage at {distance:g} m")
            elif not foreign_covered and height is not None:
                failures.append(
                    f"a height ({height:g} m) at {distance:g} m, where no "
                    "national model covers the foreign side"
                )
    if not foreign_covered:
        return failures, None
    try:
        step = seam_step(distances, heights, seam)
    except ValueError as error:
        return [*failures, str(error)], None
    if step > max_step_m:
        failures.append(f"seam step {step:.2f} m exceeds {max_step_m:g} m")
    return failures, step


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


def _load_profiles(path: Path) -> list[dict[str, Any]]:
    """Return the seam profiles listed in verify_points.json."""
    document = json.loads(path.read_text(encoding="utf-8"))
    profiles: list[dict[str, Any]] = document["profiles"]
    return profiles


def _profile_samples(profile: dict[str, Any]) -> list[tuple[float, float, float]]:
    """Return one listed profile's (distance, lon, lat) samples."""
    return profile_points(
        float(profile["lon"]),
        float(profile["lat"]),
        float(profile["bearing_deg"]),
        float(profile.get("length_m", PROFILE_LENGTH_M)),
        float(profile.get("step_m", PROFILE_STEP_M)),
    )


def _cmd_profile_points(args: argparse.Namespace) -> int:
    # Tab after the name, which has spaces in it; the rest space separated, the
    # grid coordinates last so `cut -f2 | awk '{print $4, $5}'` feeds
    # gdallocationinfo.
    for profile in _load_profiles(args.points):
        for distance, lon, lat in _profile_samples(profile):
            easting, northing = lonlat_to_grid(lon, lat)
            print(
                f"{profile['name']}\t{distance:g} {lon:.7f} {lat:.7f} "
                f"{easting:.2f} {northing:.2f}"
            )
    return 0


def fetch_tile(url: str) -> bytes | None:
    """Return a terrain tile's body, or None for the 204 that means no source."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - checked
        if response.status == 204:
            return None
        body: bytes = response.read()
    return body


def read_local_tile(path: str) -> bytes | None:
    """Return a tile body from a local tree, or None where no tile was cut.

    The build's own tree has no tile where no source covers the ground, which
    is what the origin answers 204 for; a missing file means the same here.
    """
    tile = Path(path)
    return tile.read_bytes() if tile.is_file() else None


def tile_reader(base: str) -> Callable[[str], bytes | None]:
    """Return the fetch for a tileset: HTTP for a URL, the disk for a directory.

    The build checks its own tree before uploading, with the same code verify.sh
    runs against the origin afterwards, so a failure surfaces before the upload.
    """
    if base.startswith(("http://", "https://")):
        return fetch_tile
    if not Path(base).is_dir():
        raise ValueError(f"{base} is neither an http(s) URL nor a tile directory")
    return read_local_tile


def sample_heights(
    url: str,
    points: list[tuple[float, float]],
    fetch: Callable[[str], bytes | None] = fetch_tile,
) -> list[float | None]:
    """Return the published height at each (lon, lat), None where there is none."""
    tiles: dict[tuple[int, int], bytes | None] = {}
    heights: list[float | None] = []
    for lon, lat in points:
        cell = cell_for(*lonlat_to_grid(lon, lat))
        key = (cell.tile_x, cell.tile_y)
        if key not in tiles:
            tiles[key] = fetch(f"{url.rstrip('/')}/{cell.tile_x}/{cell.tile_y}.s16")
        body = tiles[key]
        heights.append(None if body is None else read_cell(body, cell.row, cell.col))
    return heights


def _cmd_profile(args: argparse.Namespace) -> int:
    profiles = {p["name"]: p for p in _load_profiles(args.points)}
    profile = profiles[args.name]
    expectations = json.loads(args.expect.read_text(encoding="utf-8"))
    expect = expectations["profiles"][args.name]
    samples = _profile_samples(profile)
    distances = [sample[0] for sample in samples]
    heights = sample_heights(
        args.url, [(s[1], s[2]) for s in samples], tile_reader(args.url)
    )
    failures, step = assess_profile(
        distances, heights, expect, float(profile.get("max_step_m", 3.0))
    )
    for failure in failures[:10]:
        print(f"FAIL {failure}")
    if len(failures) > 10:
        print(f"FAIL ... and {len(failures) - 10} more")
    if failures:
        return 1
    if step is None:
        print("ok no national model on the foreign side; it reads no height")
    else:
        print(f"ok seam step {step:.2f} m")
    return 0


def check_summits(
    summits: list[dict[str, Any]], heights: list[float | None]
) -> list[str]:
    """Return a failure line for each summit outside its tolerance band."""
    failures = []
    for summit, height in zip(summits, heights, strict=True):
        low = summit["height_m"] - summit["tolerance"]["below_m"]
        high = summit["height_m"] + summit["tolerance"]["above_m"]
        if height is None:
            failures.append(f"{summit['name']}: no height, expected {low}-{high} m")
        elif not low <= height <= high:
            failures.append(
                f"{summit['name']}: {height:.2f} m, expected {low}-{high} m"
            )
    return failures


#: How far from a summit's listed coordinate to look for its highest cell.
#: The coordinates are public WGS84 positions, not surveyed by us, and on the
#: first Alps-wide build (20 Nov 2026) every summit's highest cell lay 10-80 m
#: from them; at the coordinate itself the grid read up to 88 m low.
SUMMIT_SEARCH_M = 100


def summit_heights(
    base: str,
    summits: list[dict[str, Any]],
    fetch: Callable[[str], bytes | None],
    radius_m: int = SUMMIT_SEARCH_M,
) -> list[float | None]:
    """Return each summit's highest stored height within ``radius_m``."""
    tiles: dict[tuple[int, int], bytes | None] = {}
    heights: list[float | None] = []
    for summit in summits:
        easting, northing = lonlat_to_grid(float(summit["lon"]), float(summit["lat"]))
        best: float | None = None
        for dx in range(-radius_m, radius_m + 1, CELL_SIZE_M):
            for dy in range(-radius_m, radius_m + 1, CELL_SIZE_M):
                if dx * dx + dy * dy > radius_m * radius_m:
                    continue
                cell = cell_for(easting + dx, northing + dy)
                key = (cell.tile_x, cell.tile_y)
                if key not in tiles:
                    tiles[key] = fetch(
                        f"{base.rstrip('/')}/{cell.tile_x}/{cell.tile_y}.s16"
                    )
                body = tiles[key]
                height = None if body is None else read_cell(body, cell.row, cell.col)
                if height is not None and (best is None or height > best):
                    best = height
        heights.append(best)
    return heights


def _print_summits(base: str, points: Path) -> list[str]:
    """Print each summit's highest nearby height; return the misses."""
    summits: list[dict[str, Any]] = json.loads(points.read_text(encoding="utf-8"))[
        "summits"
    ]
    heights = summit_heights(base, summits, tile_reader(base))
    for summit, height in zip(summits, heights, strict=True):
        shown = "no height" if height is None else f"{height:.2f} m"
        print(
            f"{summit['name']}: {shown} within {SUMMIT_SEARCH_M} m "
            f"(published {summit['height_m']} m)"
        )
    return check_summits(summits, heights)


def _cmd_summits(args: argparse.Namespace) -> int:
    # verify.sh's summit check: the highest cell near each listed summit must
    # be within its band of the agency's published height.
    failures = _print_summits(args.url, args.points)
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


def tile_exists(
    base: str, lon: float, lat: float, fetch: Callable[[str], bytes | None]
) -> bool:
    """Return whether the tileset holds a tile at (lon, lat): not a 204."""
    cell = cell_for(*lonlat_to_grid(lon, lat))
    return fetch(f"{base.rstrip('/')}/{cell.tile_x}/{cell.tile_y}.s16") is not None


def _cmd_places(args: argparse.Namespace) -> int:
    # The named places verify.sh checks after the upload, run against a local
    # tree before it. A point no source covers that holds a tile fails: the
    # build put heights where it promised none. A summit outside its band only
    # warns here; verify.sh's summit check fails on it after the upload.
    document = json.loads(args.points.read_text(encoding="utf-8"))
    fetch = tile_reader(args.url)
    for warning in _print_summits(args.url, args.points):
        print(f"WARN {warning}")

    absent: list[dict[str, Any]] = list(document.get("absent", []))
    if args.expect is not None:
        gap = json.loads(args.expect.read_text(encoding="utf-8")).get("gap")
        if gap is not None:
            absent.append(gap)
    failures = []
    for place in absent:
        if tile_exists(args.url, float(place["lon"]), float(place["lat"]), fetch):
            failures.append(f"{place['name']}: a tile exists where none should")
        else:
            print(f"{place['name']}: no tile, as expected")

    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


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

    p_points = sub.add_parser(
        "profile-points", help="every seam profile's samples, for the build"
    )
    p_points.add_argument("--points", type=Path, default=VERIFY_POINTS)
    p_points.set_defaults(func=_cmd_profile_points)

    p_profile = sub.add_parser("profile", help="check one seam profile")
    p_profile.add_argument("name", help="a profile name in verify_points.json")
    p_profile.add_argument(
        "--url", required=True, help="terrain/<version> base URL, or a local tile tree"
    )
    p_profile.add_argument(
        "--expect", type=Path, required=True, help="the build's verify-gap.json"
    )
    p_profile.add_argument("--points", type=Path, default=VERIFY_POINTS)
    p_profile.set_defaults(func=_cmd_profile)

    p_places = sub.add_parser(
        "places",
        help="no tile where no source covers; warn on a summit outside its band",
    )
    p_places.add_argument(
        "--url", required=True, help="terrain/<version> base URL, or a local tile tree"
    )
    p_places.add_argument(
        "--expect", type=Path, help="the build's verify-gap.json, for its gap point"
    )
    p_places.add_argument("--points", type=Path, default=VERIFY_POINTS)
    p_places.set_defaults(func=_cmd_places)

    p_summits = sub.add_parser(
        "summits", help="fail on a summit whose nearby highest cell is out of band"
    )
    p_summits.add_argument(
        "--url", required=True, help="terrain/<version> base URL, or a local tile tree"
    )
    p_summits.add_argument("--points", type=Path, default=VERIFY_POINTS)
    p_summits.set_defaults(func=_cmd_summits)

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
