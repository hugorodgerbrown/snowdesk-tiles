#!/usr/bin/env python3
r"""The terrain-class tileset: its pixel encoding, its kernel, and its contract.

SNOW-987. The elevation grid (``terrain_grid.py``) is read by Django one point
at a time. That is the right shape for scoring a route, and the wrong one for
the browser: drawing a route's slope under the cursor, or shading the ground
around it, needs the answer for every pixel on screen at once, and no client is
going to fetch a 133 kB Int16 tile and run a slope kernel per pixel to get it.
So this tileset precomputes the answer — height, slope band and aspect octant —
for every 5 m cell, warps it to Web Mercator, and publishes it as ordinary
256 px PNG map tiles at z12-14 that MapLibre and a canvas can both read.

**The classes are sample_slope's, exactly.** The kernel here is a copy of
``_horn`` in snowdesk-data-pipeline's ``apps/locations/services/terrain.py``,
run the way ``sample_slope`` runs it at the default 10 m window: a 3x3 Horn
kernel over cells *two* apart on the 5 m grid, one nodata cell voiding the whole
kernel, aspect a compass bearing of steepest descent. Octants are a copy of
``apps/core/geo.py``'s ``octant_for``. A pixel and a point sample that disagree
would put two answers on one screen — the server says 32 degrees, the colour
under the cursor says 25-30 — and the second answer would be the one users
believe, because it is the one they can see. The tests pin the copy against the
data-pipeline's own literal results.

**Bands and octants, not degrees.** A whole-degree slope and a 1-degree aspect
would need 16 bits between them, and nothing reading the tile wants either at
that resolution: avalanche terrain is reasoned about in 5-degree steps (30, 35,
40) and in the eight octants a bulletin names. Classes fit in one byte, so the
height gets the other two, and the whole pixel is one RGBA PNG pixel that a
browser can decode without a custom codec.

The pixel contract, published in ``tiles.json``::

    R, G   height in whole metres, uint16 big-endian (R is the high byte),
           floor(stored * 0.25 + 0.5) from the stored Int16 quarter-metres
    B      octant << 5 | band, octant N=0 .. NW=7, band = floor(angle / 5)
           in 0..17 (17 also holds anything at or past 85 degrees)
    B=254  level ground: the kernel is exactly flat, so there is no aspect.
           R, G still carry the height. Lakes are the common case.
    B=255  no data: no height, or a kernel with a hole in it. R = G = 0.
    A      always 255

The largest real B is ``7 << 5 | 17`` = 241, so 254 and 255 can never be
mistaken for a class. Alpha is fixed at 255 because a browser canvas stores
pixels premultiplied: any alpha below 255 would round the colour channels on
the way in, and the colour channels here are data, not colour.

**Level and no-data are different codes on purpose.** Flat ground is a real,
known answer — a lake is 0 degrees — and nodata is "we do not know". The one
failure the elevation grid is arranged to prevent is the second reading as the
first, and that holds here too: a missing pixel must never be painted gentle.

Standalone operational tooling: stdlib only. The vectorised classification over
the whole grid needs numpy and lives in ``classify_terrain.py``; what lives here
is the arithmetic every other part — the classifier, the tile cutter, the
acceptance checks and the browser — has to agree on, and it has to be readable
and testable on its own.

Usage:
    python scripts/terrain_class.py descriptor --origin https://host --version v1
    python scripts/terrain_class.py mercator-extent --manifest m.json --zoom 14
    python scripts/terrain_class.py pixel 1603 32.4 112.0
    python scripts/terrain_class.py locate 7.7491 46.0207 14
    python scripts/terrain_class.py expect 7.7491 46.0207 14 \
        --terrain-url https://tiles.snowdesk-data.info/terrain/v1
    curl -s .../terrain-class/v1/14/8544/5827.png \
        | python scripts/terrain_class.py read-pixel 171 113
    curl -s .../terrain-class/v1/14/8544/5827.png \
        | python scripts/terrain_class.py check-tile
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from terrain_grid import (
    ATTRIBUTION,
    CELL_SIZE_M,
    DEFAULT_ANALYSIS_WINDOW_M,
    GRID_ID,
    HEIGHT_SCALE_M,
    NODATA,
    STORED_CELLS,
    TILE_BYTES,
    cell_centre,
    cell_for,
    lonlat_to_grid,
)

# --- The contract -----------------------------------------------------------
#
# Every value below is published in tiles.json and read by the browser.
# Changing any of them changes what a published pixel means: bump
# TERRAIN_CLASS_VERSION in config.sh in the same commit, or clients holding a
# year of immutable tiles decode them under the new rules.

TILESET_ID = "snowdesk-terrain-class"

#: Zoom range the tiles exist at. z12 is the first level at which a pixel
#: (~25 m in the Alps) is within a factor of three of the analysis window; below
#: it a pixel would summarise ground the class does not describe. z14 (~6.6 m)
#: is the first at which a pixel is finer than the 5 m grid's own cells, so
#: z15+ would only repeat z14's answers and is left to overzoom.
#: The Worker answers 204 outside this range — keep it in step.
MIN_ZOOM = 12
MAX_ZOOM = 14

TILE_PIXELS = 256

#: Slope band width in degrees. 30, 35 and 40 — the thresholds avalanche terrain
#: is reasoned about in — are all band edges.
BAND_DEG = 5
#: Bands 0..17. 90 / 5 is 18, but only a vertical wall reaches 90 degrees, so
#: the last band is folded down to 17 rather than given a code of its own.
BAND_COUNT = 18
MAX_BAND = BAND_COUNT - 1

#: The CAAML enum's own spellings, in compass order from north — the same tuple
#: as ``apps/core/geo.py``, so an octant here is the string a bulletin uses.
OCTANTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
OCTANT_DEG = 360.0 / len(OCTANTS)
OCTANT_SHIFT = 5

LEVEL = 254
NO_DATA = 255
ALPHA = 255
NODATA_PIXEL = (0, 0, NO_DATA, ALPHA)

#: The kernel's reach, in cells. The default analysis window over the storage
#: spacing: 10 m / 5 m = 2, so the 3x3 kernel samples cells two apart.
WINDOW_STEP_CELLS = DEFAULT_ANALYSIS_WINDOW_M // CELL_SIZE_M
WINDOW_SPACING_M = WINDOW_STEP_CELLS * CELL_SIZE_M

#: Kernel offsets as (row, col) in cells, row-major from the north-west — the
#: order ``horn`` takes its heights in, with rows running north to south.
KERNEL_OFFSETS = tuple(
    (row * WINDOW_STEP_CELLS, col * WINDOW_STEP_CELLS)
    for row in (-1, 0, 1)
    for col in (-1, 0, 1)
)

#: Highest height the two colour channels can carry.
MAX_HEIGHT_M = 0xFFFF

#: Half the Web Mercator world's width in metres: EPSG:3857's extent is
#: [-ORIGIN_SHIFT_M, ORIGIN_SHIFT_M] on both axes.
ORIGIN_SHIFT_M = math.pi * 6378137.0

if ((len(OCTANTS) - 1) << OCTANT_SHIFT | MAX_BAND) >= LEVEL:
    raise AssertionError("a real class would collide with LEVEL or NO_DATA")


class TerrainClass(NamedTuple):
    """One decoded pixel: what the tile says about one patch of ground.

    ``height_m`` is None only for no data. ``octant`` is None both for no data
    and for level ground, which is told apart by ``band`` being 0 rather than
    None — level ground has a known angle, and it is zero.
    """

    height_m: int | None
    band: int | None
    octant: str | None

    @property
    def is_nodata(self) -> bool:
        """True where the tile has no answer at all."""
        return self.height_m is None

    @property
    def is_level(self) -> bool:
        """True on ground that is exactly flat, and so faces nowhere."""
        return self.height_m is not None and self.octant is None


NODATA_CLASS = TerrainClass(None, None, None)


# --- The kernel ---------------------------------------------------------------


def horn(heights: Sequence[float], spacing_m: float) -> tuple[float, float | None]:
    """Return the slope angle and aspect of a 3x3 kernel by Horn's method.

    A copy of ``_horn`` in snowdesk-data-pipeline's
    ``apps/locations/services/terrain.py``, operation for operation, because
    the classes have to equal ``sample_slope``'s and floating point is only
    reproducible when the arithmetic is. Do not tidy it.

    ``heights`` is row-major from the kernel's north-west corner, so index 0 is
    north-west, 4 is the centre and 8 is south-east. The aspect is a compass
    bearing — the direction of steepest descent, degrees clockwise from north —
    and is None on exactly level ground.
    """
    north_west, north, north_east, west, _, east, south_west, south, south_east = (
        heights
    )

    # Eastward gradient: the east column against the west, centre-weighted.
    east_gradient = (
        (north_east + 2 * east + south_east) - (north_west + 2 * west + south_west)
    ) / (8 * spacing_m)
    # Southward gradient: per row of the kernel, so downhill in +row.
    south_gradient = (
        (south_west + 2 * south + south_east) - (north_west + 2 * north + north_east)
    ) / (8 * spacing_m)

    angle = math.degrees(math.atan(math.hypot(east_gradient, south_gradient)))
    if not east_gradient and not south_gradient:
        return 0.0, None
    aspect = math.degrees(math.atan2(-east_gradient, south_gradient)) % 360
    return angle, aspect


def to_metres(stored: int) -> float:
    """Convert a stored Int16 to metres, as ``sample_slope``'s ``_to_metres``.

    The published grid's offset is 0, and the data pipeline adds it anyway; an
    integer zero added to a float changes nothing, so it is left out here.
    """
    return stored * HEIGHT_SCALE_M


# --- Classes ------------------------------------------------------------------


def octant_index(aspect_deg: float | None) -> int | None:
    """Return the index into ``OCTANTS`` of a compass bearing, or None.

    A copy of ``octant_for`` in snowdesk-data-pipeline's ``apps/core/geo.py``,
    returning the index rather than the name. The octants are centred on their
    own names — N is 337.5 up to (not including) 22.5 — because that is what a
    bulletin means by "north facing".
    """
    if aspect_deg is None:
        return None
    return int(((aspect_deg % 360.0) + OCTANT_DEG / 2) // OCTANT_DEG) % len(OCTANTS)


def band_for(angle_deg: float) -> int:
    """Return the 5-degree slope band an angle falls in, 0..17.

    Floor, so a band is half-open: 30.0 is in the 30-35 band, 29.999 in 25-30.
    That is the reading a threshold needs — "30 degrees and over" starts at 30.
    """
    if angle_deg < 0:
        raise ValueError(f"slope angle {angle_deg} is negative")
    return min(int(angle_deg // BAND_DEG), MAX_BAND)


def class_byte(angle_deg: float, aspect_deg: float | None) -> int:
    """Return the B channel for a slope: ``octant << 5 | band``, or LEVEL."""
    octant = octant_index(aspect_deg)
    if octant is None:
        return LEVEL
    return octant << OCTANT_SHIFT | band_for(angle_deg)


def height_metres(stored: int) -> int:
    """Return a stored Int16 height in whole metres, rounded half up.

    ``floor(x + 0.5)`` rather than Python's ``round``, which rounds half to
    even: a quarter-metre grid lands exactly on .5 a quarter of the time, and
    banker's rounding would send half of those down and half up depending on
    whether the metre below is odd. Stated in the contract so the browser can
    say what a height means to the metre.
    """
    return math.floor(stored * HEIGHT_SCALE_M + 0.5)


def encode_pixel(height_m: int | None, b: int) -> tuple[int, int, int, int]:
    """Pack a height and a class byte into one RGBA pixel."""
    if height_m is None or b == NO_DATA:
        return NODATA_PIXEL
    if not 0 <= height_m <= MAX_HEIGHT_M:
        # Below sea level is not a quirk to clamp: no ground in the Alps is,
        # and a negative height here means the stored scale was misread.
        raise ValueError(f"height {height_m} m does not fit in 16 unsigned bits")
    # Validates rather than decodes: raises on a byte no class produces.
    decode_class_byte(b)
    return height_m >> 8, height_m & 0xFF, b, ALPHA


def decode_class_byte(b: int) -> tuple[int | None, str | None]:
    """Return ``(band, octant)`` for a B channel value.

    Level ground is ``(0, None)``; no data is ``(None, None)``. Raises on a
    value no encoder writes, which is what a resampled or recompressed tile
    would produce — better a loud error than a plausible class.
    """
    if b == NO_DATA:
        return None, None
    if b == LEVEL:
        return 0, None
    octant, band = b >> OCTANT_SHIFT, b & ((1 << OCTANT_SHIFT) - 1)
    if not 0 <= b < LEVEL or octant >= len(OCTANTS) or band > MAX_BAND:
        raise ValueError(f"class byte {b} is not a valid class")
    return band, OCTANTS[octant]


def decode_pixel(r: int, g: int, b: int, a: int) -> TerrainClass:
    """Decode one RGBA pixel into a ``TerrainClass``."""
    if a != ALPHA:
        raise ValueError(
            f"alpha is {a}, not {ALPHA} — the tile was composited or resampled "
            "somewhere between the build and here"
        )
    band, octant = decode_class_byte(b)
    if band is None:
        if r or g:
            raise ValueError(f"no-data pixel carries a height ({r}, {g})")
        return NODATA_CLASS
    return TerrainClass((r << 8) | g, band, octant)


def classify_kernel(stored: Sequence[int]) -> tuple[int, int, int, int]:
    """Return the pixel for a 3x3 kernel of stored Int16 values.

    The scalar reference the vectorised classifier is tested against at every
    cell. ``stored`` is row-major from the north-west at ``KERNEL_OFFSETS``,
    so index 4 is the cell the pixel describes — and since it is one of the
    nine, a hole under the pixel itself voids it like any other.
    """
    if len(stored) != len(KERNEL_OFFSETS):
        raise ValueError(f"kernel has {len(stored)} cells, expected 9")
    # One hole voids the whole kernel, as in sample_slope: a gradient computed
    # from eight heights and a guess is a fabrication, and a plausible one.
    if any(value == NODATA for value in stored):
        return NODATA_PIXEL
    angle, aspect = horn([to_metres(value) for value in stored], WINDOW_SPACING_M)
    return encode_pixel(height_metres(stored[4]), class_byte(angle, aspect))


def describe(terrain_class: TerrainClass) -> str:
    """Return a one-line reading of a class, for the CLI and verify.sh.

    One format for both sides of a comparison, so the acceptance check can
    compare strings and print something a person can read when they differ.
    """
    if terrain_class.height_m is None or terrain_class.band is None:
        return "nodata"
    low = terrain_class.band * BAND_DEG
    facing = terrain_class.octant or "level"
    return f"{terrain_class.height_m} m, band {low}-{low + BAND_DEG}, {facing}"


# --- Web Mercator -------------------------------------------------------------
#
# The tiles are cut on the standard XYZ scheme so MapLibre can address them
# like any raster source. The arithmetic is the usual spherical Mercator, in
# stdlib for the same reason the LAEA projection in terrain_grid.py is: it is
# all this repo needs of PROJ.


class MercatorExtent(NamedTuple):
    """A tile-aligned rectangle at one zoom, in EPSG:3857 metres and tiles."""

    zoom: int
    west: float
    south: float
    east: float
    north: float
    tile_x: int
    tile_y: int
    tiles_x: int
    tiles_y: int

    @property
    def width(self) -> int:
        """Raster width in pixels."""
        return self.tiles_x * TILE_PIXELS

    @property
    def height(self) -> int:
        """Raster height in pixels."""
        return self.tiles_y * TILE_PIXELS


def mercator_strips(
    extent: MercatorExtent, strip_tiles: int
) -> list[tuple[float, float, int]]:
    """Split an extent into horizontal strips of whole tile rows, north first.

    Returns ``(south, north, height_px)`` per strip. Each strip is warped on its
    own and the strips are concatenated: a BIP raster's rows are contiguous, so
    the strips laid end to end are byte for byte the raster of the whole
    extent. Edges are computed from the extent's own north and pixel size, so
    neighbouring strips share an edge exactly and the last ends on ``south``.
    """
    if strip_tiles < 1:
        raise ValueError(f"strip_tiles must be at least 1, not {strip_tiles}")
    pixel = (extent.north - extent.south) / extent.height
    strips = []
    for first in range(0, extent.tiles_y, strip_tiles):
        last = min(first + strip_tiles, extent.tiles_y)
        top = first * TILE_PIXELS
        bottom = last * TILE_PIXELS
        north = extent.north - top * pixel
        south = (
            extent.south if last == extent.tiles_y else extent.north - bottom * pixel
        )
        strips.append((south, north, bottom - top))
    return strips


def world_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Return the global pixel coordinate of a WGS84 point at a zoom."""
    size = TILE_PIXELS * 2**zoom
    x = (lon + 180.0) / 360.0 * size
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * size
    return x, y


def pixel_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Return the WGS84 point at a global pixel coordinate — the inverse."""
    size = TILE_PIXELS * 2**zoom
    lon = x / size * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
    return lon, lat


def mercator_extent(
    west: float,
    south: float,
    east: float,
    north: float,
    zoom: int,
    margin_tiles: int = 1,
) -> MercatorExtent:
    """Snap a WGS84 box out to whole XYZ tiles at a zoom, with a margin.

    The margin is cheap insurance against the box being a hair inside the
    data: it comes from the STAC catalogue's per-square WGS84 boxes, not from
    the warped grid, and an edge tile cropped by one pixel would be a seam.
    Tiles with nothing in them are never written, so the extra ring costs a
    few seconds of warp and no storage.
    """
    limit = 2**zoom - 1
    x0, y0 = world_pixel(west, north, zoom)
    x1, y1 = world_pixel(east, south, zoom)
    tile_x = max(math.floor(x0 / TILE_PIXELS) - margin_tiles, 0)
    tile_y = max(math.floor(y0 / TILE_PIXELS) - margin_tiles, 0)
    last_x = min(math.floor(x1 / TILE_PIXELS) + margin_tiles, limit)
    last_y = min(math.floor(y1 / TILE_PIXELS) + margin_tiles, limit)
    tile_m = 2 * ORIGIN_SHIFT_M / 2**zoom
    return MercatorExtent(
        zoom=zoom,
        west=-ORIGIN_SHIFT_M + tile_x * tile_m,
        south=ORIGIN_SHIFT_M - (last_y + 1) * tile_m,
        east=-ORIGIN_SHIFT_M + (last_x + 1) * tile_m,
        north=ORIGIN_SHIFT_M - tile_y * tile_m,
        tile_x=tile_x,
        tile_y=tile_y,
        tiles_x=last_x - tile_x + 1,
        tiles_y=last_y - tile_y + 1,
    )


def locate(lon: float, lat: float, zoom: int) -> tuple[int, int, int, int, int]:
    """Return ``(zoom, tile_x, tile_y, px, py)`` for the pixel holding a point."""
    x, y = world_pixel(lon, lat, zoom)
    gx, gy = math.floor(x), math.floor(y)
    return (
        zoom,
        gx // TILE_PIXELS,
        gy // TILE_PIXELS,
        gx % TILE_PIXELS,
        gy % TILE_PIXELS,
    )


# --- The published descriptor -------------------------------------------------


def descriptor(
    origin: str = "",
    version: str = "",
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the tileset descriptor published as ``tiles.json``.

    TileJSON 3.0.0, so MapLibre can take it as a raster source's ``url``, with
    the pixel contract alongside under ``encoding``. The contract travels with
    the tiles for the same reason ``grid.json`` does: the reader is in another
    repository, and a decoding rule that lives only in code is one that drifts.
    """
    doc: dict[str, Any] = {
        "tilejson": "3.0.0",
        "name": TILESET_ID,
        "scheme": "xyz",
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "tile_size": TILE_PIXELS,
        "format": "png",
        "attribution": ATTRIBUTION,
        "encoding": {
            "height": (
                "R and G: height in whole metres as uint16, R the high byte; "
                f"floor(stored * {HEIGHT_SCALE_M} + 0.5) from the {GRID_ID} grid"
            ),
            "class": (
                f"B: octant << {OCTANT_SHIFT} | band; band = floor(angle / "
                f"{BAND_DEG}) in 0..{MAX_BAND}, the last band holding everything "
                f"from {MAX_BAND * BAND_DEG} degrees up"
            ),
            "octants": list(OCTANTS),
            "band_deg": BAND_DEG,
            "bands": BAND_COUNT,
            "level": LEVEL,
            "level_meaning": (
                "exactly level ground: no aspect, angle 0; R and G carry the height"
            ),
            "nodata": NO_DATA,
            "nodata_meaning": (
                "no height, or a kernel with a hole in it; R = G = 0. Never level "
                "ground."
            ),
            "alpha": ALPHA,
            "source_grid": GRID_ID,
            "analysis_window_m": WINDOW_SPACING_M,
            "kernel": (
                f"Horn 3x3 over cells {WINDOW_STEP_CELLS} apart on the "
                f"{CELL_SIZE_M} m grid; one nodata cell voids the kernel. Equal "
                "to sample_slope at its default window."
            ),
            "resampling": "nearest, from EPSG:3035 cells to Web Mercator pixels",
        },
        "absent_tile": (
            "204 No Content means no ground in this tile has data. It is not an "
            "error and must never be read as level terrain."
        ),
    }
    if version:
        doc["version"] = version
    if origin and version:
        doc["tiles"] = [
            f"{origin.rstrip('/')}/terrain-class/{version}/{{z}}/{{x}}/{{y}}.png"
        ]
    if coverage:
        doc.update(coverage)
    return doc


# --- PNG reading --------------------------------------------------------------
#
# Only what verify.sh needs: one pixel out of a tile fetched from the origin.
# The writer lives in cut_terrain_class_tiles.py and only ever emits filter 0,
# but this reads every filter type, so a tile that was somehow re-encoded on
# the way decodes rather than failing for an uninteresting reason.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    to_left, to_up, to_corner = (
        abs(estimate - left),
        abs(estimate - up),
        abs(estimate - up_left),
    )
    if to_left <= to_up and to_left <= to_corner:
        return left
    return up if to_up <= to_corner else up_left


def read_png_rgba(body: bytes) -> tuple[int, int, list[bytes]]:
    """Decode an 8-bit RGBA, non-interlaced PNG into its rows of pixels."""
    if not body.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    offset = len(PNG_SIGNATURE)
    header: tuple[int, ...] = ()
    data = bytearray()
    while offset < len(body):
        (length,) = struct.unpack_from(">I", body, offset)
        kind = body[offset + 4 : offset + 8]
        chunk = body[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            data += chunk
        elif kind == b"IEND":
            break
    if not header:
        raise ValueError("PNG has no IHDR")
    width, height, depth, colour, _, _, interlace = header
    if (depth, colour, interlace) != (8, 6, 0):
        raise ValueError(
            f"PNG is depth {depth}, colour type {colour}, interlace {interlace}; "
            "the contract is 8-bit RGBA, not interlaced"
        )
    raw = zlib.decompress(bytes(data))
    stride = width * 4
    rows: list[bytes] = []
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        filter_type = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if filter_type > 4:
            raise ValueError(f"unknown PNG filter type {filter_type}")
        for i in range(stride if filter_type else 0):
            left = line[i - 4] if i >= 4 else 0
            up = previous[i]
            up_left = previous[i - 4] if i >= 4 else 0
            if filter_type == 1:
                line[i] = (line[i] + left) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + up) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            else:
                line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF
        rows.append(bytes(line))
        previous = line
    return width, height, rows


def read_pixel(body: bytes, px: int, py: int) -> TerrainClass:
    """Decode the class of one pixel of a published tile."""
    width, height, rows = read_png_rgba(body)
    if not (0 <= px < width and 0 <= py < height):
        raise ValueError(f"pixel ({px}, {py}) is outside the {width}x{height} tile")
    r, g, b, a = rows[py][px * 4 : px * 4 + 4]
    return decode_pixel(r, g, b, a)


# --- The expected answer ------------------------------------------------------

#: Fetch one elevation tile by (tile_x, tile_y); None where none is published.
TileFetcher = Callable[[int, int], bytes | None]


def kernel_at(easting: float, northing: float, fetch: TileFetcher) -> list[int]:
    """Return the nine stored values of the kernel centred on a coordinate.

    Each neighbour is addressed through its own cell centre, so the owning tile
    and the row and column inside it come from the published addressing rather
    than from arithmetic on one tile's skirt. That matters: the skirt is one
    cell and the kernel reaches two, so a kernel near a tile edge reads up to
    four tiles. An absent tile is nodata, as it is to sample_slope.
    """
    tiles: dict[tuple[int, int], bytes | None] = {}
    stored: list[int] = []
    for row_offset, col_offset in KERNEL_OFFSETS:
        neighbour = cell_for(
            easting + col_offset * CELL_SIZE_M, northing - row_offset * CELL_SIZE_M
        )
        key = (neighbour.tile_x, neighbour.tile_y)
        if key not in tiles:
            tiles[key] = fetch(*key)
        body = tiles[key]
        if body is None:
            stored.append(NODATA)
            continue
        if len(body) != TILE_BYTES:
            raise ValueError(f"terrain tile {key} is {len(body)} bytes")
        offset = (neighbour.row * STORED_CELLS + neighbour.col) * 2
        stored.append(struct.unpack_from("<h", body, offset)[0])
    return stored


def expected_class(lon: float, lat: float, zoom: int, fetch: TileFetcher) -> str:
    """Return what the class tile *should* say at the pixel holding a point.

    Computed from the published elevation tiles, with the scalar kernel, at the
    centre of the Mercator pixel rather than at the point itself — the warp is
    nearest-neighbour, so the pixel carries the class of whichever 5 m cell its
    centre lands in, and at z14 that is not always the cell holding the probe.

    This is what makes verify.sh an end-to-end check rather than a sanity one:
    the class tileset is compared with the height tileset that SNOW-917 samples,
    through the same kernel, so a class tile that disagrees with sample_slope
    fails here before it disagrees on a screen.
    """
    _, tile_x, tile_y, px, py = locate(lon, lat, zoom)
    centre_lon, centre_lat = pixel_lonlat(
        tile_x * TILE_PIXELS + px + 0.5, tile_y * TILE_PIXELS + py + 0.5, zoom
    )
    cell = cell_for(*lonlat_to_grid(centre_lon, centre_lat))
    stored = kernel_at(*cell_centre(*cell), fetch)
    return describe(decode_pixel(*classify_kernel(stored)))


def http_fetcher(terrain_url: str) -> TileFetcher:
    """Return a fetcher reading elevation tiles from a published origin."""
    base = terrain_url.rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError(f"terrain URL {terrain_url!r} is not http(s)")

    def fetch(tile_x: int, tile_y: int) -> bytes | None:
        url = f"{base}/{tile_x}/{tile_y}.s16"
        # The tile host's Cloudflare zone answers 403 to urllib's default
        # User-Agent, as OpenFreeMap does (see rewrite_style.py).
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url, headers={"User-Agent": "snowdesk-tiles"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - scheme checked above
            if response.status == 204:
                return None
            body: bytes = response.read()
            return body

    return fetch


# --- CLI ----------------------------------------------------------------------


def _cmd_descriptor(args: argparse.Namespace) -> int:
    json.dump(descriptor(args.origin, args.version), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _extent_from_args(args: argparse.Namespace) -> MercatorExtent:
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        west, south, east, north = manifest["bbox"]
    else:
        west, south, east, north = args.bbox
    return mercator_extent(west, south, east, north, args.zoom)


def _cmd_mercator_strips(args: argparse.Namespace) -> int:
    # Full precision, as for mercator-extent: these become gdalwarp's -te.
    for south, north, height in mercator_strips(
        _extent_from_args(args), args.strip_tiles
    ):
        print(repr(south), repr(north), height)
    return 0


def _cmd_mercator_extent(args: argparse.Namespace) -> int:
    extent = _extent_from_args(args)
    # Full precision: these become gdalwarp's -te, and a pixel grid that is off
    # by a rounding error is off by a fraction of a pixel everywhere.
    print(
        repr(extent.west),
        repr(extent.south),
        repr(extent.east),
        repr(extent.north),
        extent.width,
        extent.height,
        extent.tile_x,
        extent.tile_y,
        extent.tiles_x,
        extent.tiles_y,
    )
    return 0


def _cmd_pixel(args: argparse.Namespace) -> int:
    aspect = None if args.aspect.lower() == "none" else float(args.aspect)
    print(*encode_pixel(args.height, class_byte(args.angle, aspect)))
    return 0


def _cmd_locate(args: argparse.Namespace) -> int:
    print(*locate(args.lon, args.lat, args.zoom))
    return 0


def _cmd_read_pixel(args: argparse.Namespace) -> int:
    body = sys.stdin.buffer.read()
    try:
        print(describe(read_pixel(body, args.px, args.py)))
    except (ValueError, zlib.error) as error:
        # Distinguishable from any class: a broken tile is a broken publish.
        print(f"broken: {error}")
        return 1
    return 0


def check_tile(body: bytes) -> int:
    """Decode every pixel of a tile and return how many carry data.

    Raises on the first pixel that breaks the contract — a translucent one, a
    class byte no encoder writes, a no-data pixel carrying a height — which is
    what a tile re-encoded, resampled or composited on its way out looks like.
    """
    width, height, rows = read_png_rgba(body)
    if (width, height) != (TILE_PIXELS, TILE_PIXELS):
        raise ValueError(f"tile is {width}x{height}, not {TILE_PIXELS} square")
    data = 0
    for row in rows:
        for offset in range(0, len(row), 4):
            r, g, b, a = row[offset : offset + 4]
            if not decode_pixel(r, g, b, a).is_nodata:
                data += 1
    return data


def _cmd_check_tile(args: argparse.Namespace) -> int:
    try:
        data = check_tile(sys.stdin.buffer.read())
    except (ValueError, zlib.error) as error:
        print(f"broken: {error}")
        return 1
    print(f"ok {data}")
    return 0


def _cmd_expect(args: argparse.Namespace) -> int:
    try:
        print(expected_class(args.lon, args.lat, args.zoom, http_fetcher(args.url)))
    except (ValueError, urllib.error.URLError) as error:
        print(f"broken: {error}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — dispatch to one of the tileset queries."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_desc = sub.add_parser("descriptor", help="emit tiles.json")
    p_desc.add_argument("--origin", default="", help="public origin serving tiles")
    p_desc.add_argument("--version", default="", help="tile URL version segment")
    p_desc.set_defaults(func=_cmd_descriptor)

    p_ext = sub.add_parser("mercator-extent", help="snap a WGS84 box to XYZ tiles")
    p_ext.add_argument("--manifest", help="fetch manifest to read the bbox from")
    p_ext.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("W", "S", "E", "N"),
        help="WGS84 bounding box, if no manifest",
    )
    p_ext.add_argument("--zoom", type=int, required=True)
    p_ext.set_defaults(func=_cmd_mercator_extent)

    p_strip = sub.add_parser(
        "mercator-strips", help="split a zoom's extent into strips of tile rows"
    )
    p_strip.add_argument("--manifest", required=True, help="fetch manifest")
    p_strip.add_argument("--zoom", type=int, required=True)
    p_strip.add_argument("--strip-tiles", type=int, required=True)
    p_strip.set_defaults(func=_cmd_mercator_strips)

    p_pix = sub.add_parser("pixel", help="encode height, angle, aspect as RGBA")
    p_pix.add_argument("height", type=int, help="whole metres")
    p_pix.add_argument("angle", type=float, help="slope in degrees")
    p_pix.add_argument("aspect", help="bearing in degrees, or 'none' for level")
    p_pix.set_defaults(func=_cmd_pixel)

    p_loc = sub.add_parser("locate", help="lon/lat to zoom, tile x, y, pixel x, y")
    p_loc.add_argument("lon", type=float)
    p_loc.add_argument("lat", type=float)
    p_loc.add_argument("zoom", type=int)
    p_loc.set_defaults(func=_cmd_locate)

    p_read = sub.add_parser("read-pixel", help="decode one pixel of a PNG on stdin")
    p_read.add_argument("px", type=int)
    p_read.add_argument("py", type=int)
    p_read.set_defaults(func=_cmd_read_pixel)

    p_chk = sub.add_parser("check-tile", help="decode every pixel of a PNG on stdin")
    p_chk.set_defaults(func=_cmd_check_tile)

    p_exp = sub.add_parser(
        "expect", help="the class a pixel should hold, from the elevation tiles"
    )
    p_exp.add_argument("lon", type=float)
    p_exp.add_argument("lat", type=float)
    p_exp.add_argument("zoom", type=int)
    p_exp.add_argument(
        "--url", required=True, help="elevation tile base, e.g. ORIGIN/terrain/v1"
    )
    p_exp.set_defaults(func=_cmd_expect)

    args = parser.parse_args(argv)
    if args.command == "mercator-extent" and not (args.manifest or args.bbox):
        parser.error("mercator-extent needs --manifest or --bbox")
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
