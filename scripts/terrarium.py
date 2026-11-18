#!/usr/bin/env python3
r"""List, decode and mosaic the Terrarium tiles the terrain grid is filled from.

SNOW-1505. Outside swissALTI3D's coverage the terrain grid takes its heights
from our own copy of Mapterhorn's tiles (``/dem/<version>/``, SNOW-1444): 512 px
lossless WebP in Web Mercator, each pixel a height in Terrarium encoding,
``R * 256 + G + B / 256 - 32768`` metres. The build reads z14 — 3.3 m pixels in
the Alps, under the grid's 5 m cell, and a 0.125 m vertical step, finer than
the grid's 0.25 m (docs/research/mapterhorn in snowdesk-data-pipeline).

Three jobs, all of them build-time:

* **Which tiles.** ``list`` turns the supported area (a union of z12 tiles,
  written by snowdesk-data-pipeline's ``export_supported_area``) and a box into
  the z14 tiles to fetch. A tile is in when its centre is inside the area and
  it overlaps the box. The area is made of whole z12 tiles, so at any zoom from
  12 up the centre test is exact rather than an approximation.
* **What they hold.** ``decode`` reads each fetched tile with Pillow and turns
  its pixels into Float32 metres. A tile that was never fetched, or came back
  empty (our origin answers 204 outside the copy), is no data — never 0 m.
* **Something GDAL can read.** The heights are written as raw Float32 blocks
  of tiles, each with a small hand-written VRT, under one mosaic VRT carrying
  EPSG:3857 and the geotransform. ``gdalwarp`` takes the mosaic straight onto
  the 5 m EPSG:3035 grid. No GDAL Python bindings: the XML is a page of text,
  and a binary dependency for it would be the only one in the repo.

Usage:
    python scripts/terrarium.py list dem/supported-area.geojson \
        --zoom 14 --bbox 5.0 43.7 16.6 48.4 > work/terrain/mapterhorn/tiles.txt
    python scripts/terrarium.py decode --tiles work/terrain/mapterhorn/tiles.txt \
        --tile-dir work/terrain/mapterhorn/webp --out work/terrain/mapterhorn/blocks
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Iterator, Sequence
from html import escape
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from terrain_grid import HEIGHT_MIN_M, HEIGHT_NODATA_M

#: The zoom the terrain build reads. See the module docstring for why.
ZOOM = 14

#: Mapterhorn's tiles are 512 px. A tile of any other size is refused rather
#: than stretched: it would be a different source, not a resized one.
TILE_PIXELS = 512

#: Half the Web Mercator world width, in metres.
MERCATOR_HALF = 20037508.342789244

#: Tiles per side of one decoded block: 8 x 8 tiles of 512 px is a 4096 px
#: square, 64 MB of Float32 — big enough that the mosaic VRT stays a few
#: thousand sources long over the Alps, small enough to hold in memory.
BLOCK_TILES = 8

#: The no-data value of the decoded heights. The grid's own float sentinel, so
#: the second warp in build-terrain.sh can use one -dstnodata for both sources.
NODATA_M = HEIGHT_NODATA_M

Ring = list[tuple[float, float]]
Box = tuple[float, float, float, float]
Tile = tuple[int, int]


# --- Decoding ---------------------------------------------------------------


def decode_terrarium(rgb: NDArray[np.uint8]) -> NDArray[np.float32]:
    """Return Terrarium heights in metres from an (rows, cols, 3) RGB array.

    Computed in float64 and narrowed once: B / 256 is exact in either, but the
    sum of a 32,768 m offset and a 1/256 m fraction is not exact in float32 at
    every height, and the narrowing should round once, not three times.
    """
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError(f"expected an RGB array, got shape {rgb.shape}")
    values = rgb[..., :3].astype(np.float64)
    metres = values[..., 0] * 256.0 + values[..., 1] + values[..., 2] / 256.0
    metres -= 32768.0
    heights = metres.astype(np.float32)
    # Below the grid's representable range is not ground: Terrarium's all-black
    # pixel decodes to -32768 m, which is what an encoder writes for "nothing".
    heights[metres < HEIGHT_MIN_M] = NODATA_M
    return heights


def read_tile(path: Path, tile_px: int = TILE_PIXELS) -> NDArray[np.float32] | None:
    """Return one tile's heights, or ``None`` if it is missing or empty.

    Empty is what the build's fetch leaves for a 204: our origin's answer for a
    tile outside the copy. A tile of the wrong size is an error, not no data.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with Image.open(path) as image:
        if image.size != (tile_px, tile_px):
            raise ValueError(f"{path} is {image.size}, expected {tile_px} px square")
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return decode_terrarium(rgb)


# --- Web Mercator tile arithmetic --------------------------------------------


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Return the fractional (x, y) tile coordinate of a point at ``zoom``."""
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Return the (lon, lat) of a fractional tile coordinate at ``zoom``."""
    n = 2**zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def tile_lonlat_bounds(x: int, y: int, zoom: int) -> Box:
    """Return a tile's (west, south, east, north) in degrees."""
    west, north = tile_to_lonlat(x, y, zoom)
    east, south = tile_to_lonlat(x + 1, y + 1, zoom)
    return west, south, east, north


def tile_size_m(zoom: int) -> float:
    """Return the side of one Web Mercator tile at ``zoom``, in metres."""
    return 2 * MERCATOR_HALF / (1 << zoom)


# --- Which tiles ------------------------------------------------------------


def _polygons(geometry: dict[str, Any]) -> Iterator[list[list[list[float]]]]:
    """Yield each polygon's rings from a Polygon or MultiPolygon geometry."""
    kind = geometry["type"]
    if kind == "Polygon":
        yield geometry["coordinates"]
    elif kind == "MultiPolygon":
        yield from geometry["coordinates"]
    else:
        raise ValueError(f"unsupported geometry type {kind!r}")


def geometries(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the geometries of a FeatureCollection, a Feature or a geometry."""
    if document.get("type") == "FeatureCollection":
        return [f["geometry"] for f in document["features"] if f.get("geometry")]
    if document.get("type") == "Feature":
        return [document["geometry"]]
    return [document]


def rings(document: dict[str, Any]) -> list[Ring]:
    """Return every ring, outer and hole, of every polygon in a GeoJSON document.

    Holes are returned alongside the outer rings: the scanline below fills by
    the even-odd rule, which makes a hole a hole without being told.
    """
    found: list[Ring] = []
    for geometry in geometries(document):
        for polygon in _polygons(geometry):
            for ring in polygon:
                found.append([(float(p[0]), float(p[1])) for p in ring])
    if not found:
        raise ValueError("the area has no polygons")
    return found


def scanline_cells(tile_rings: Sequence[Ring]) -> set[Tile]:
    """Return the integer cells whose centres fall inside rings in cell space.

    ``tile_rings`` are already in fractional tile coordinates. Even-odd fill:
    for each row, the edges crossing its centre line are found with an active
    edge list (edges sorted by their top, retired past their bottom), so the
    cost is the number of edges plus the crossings, not rows times edges — a
    national coverage polygon has hundreds of thousands of edges.
    """
    edges: list[tuple[float, float, float, float]] = []
    for ring in tile_rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
            if y0 == y1:
                continue
            if y0 > y1:
                x0, y0, x1, y1 = x1, y1, x0, y0
            edges.append((y0, y1, x0, x1))
    if not edges:
        return set()
    edges.sort()
    top = math.floor(edges[0][0])
    bottom = math.ceil(max(edge[1] for edge in edges))

    cells: set[Tile] = set()
    active: list[tuple[float, float, float, float]] = []
    pending = 0
    for row in range(top, bottom):
        centre = row + 0.5
        while pending < len(edges) and edges[pending][0] <= centre:
            active.append(edges[pending])
            pending += 1
        active = [edge for edge in active if edge[1] > centre]
        crossings = sorted(
            x0 + (centre - y0) * (x1 - x0) / (y1 - y0)
            for y0, y1, x0, x1 in active
            if y0 <= centre < y1
        )
        for left, right in zip(crossings[::2], crossings[1::2], strict=False):
            for col in range(math.ceil(left - 0.5), math.floor(right - 0.5) + 1):
                cells.add((col, row))
    return cells


def to_tile_space(area_rings: Iterable[Ring], zoom: int) -> list[Ring]:
    """Return rings in lon/lat converted to fractional tile coordinates."""
    return [
        [lonlat_to_tile(lon, lat, zoom) for lon, lat in ring] for ring in area_rings
    ]


def _overlaps(a: Box, b: Box) -> bool:
    """Whether two boxes share some area; touching along an edge does not count."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def tiles_for_area(
    area: dict[str, Any], zoom: int, bbox: Box | None = None
) -> list[Tile]:
    """Return the tiles at ``zoom`` whose centres are inside the area.

    With ``bbox``, only those that also overlap it. Sorted by (x, y), so the
    fetch list and the mosaic are stable from run to run.
    """
    cells = scanline_cells(to_tile_space(rings(area), zoom))
    if bbox is not None:
        cells = {
            cell for cell in cells if _overlaps(tile_lonlat_bounds(*cell, zoom), bbox)
        }
    return sorted(cells)


def tiles_for_box(box: Box, zoom: int) -> list[Tile]:
    """Return every tile at ``zoom`` that overlaps a (west, south, east, north) box.

    Unlike ``tiles_for_area``, which takes a tile only when its centre is inside
    the area, this takes any tile the box touches. A box smaller than a tile
    (a datum-check window, say) may hold no tile centre at all.
    """
    west, south, east, north = box
    x0, y0 = lonlat_to_tile(west, north, zoom)
    x1, y1 = lonlat_to_tile(east, south, zoom)
    last = (1 << zoom) - 1
    xs = range(max(0, math.floor(x0)), min(last, math.ceil(x1) - 1) + 1)
    ys = range(max(0, math.floor(y0)), min(last, math.ceil(y1) - 1) + 1)
    return [(x, y) for x in xs for y in ys]


def tiles_bbox(tiles: Iterable[Tile], zoom: int) -> Box:
    """Return the (west, south, east, north) in degrees of a set of tiles."""
    xs, ys = zip(*tiles, strict=True)
    west, north = tile_to_lonlat(min(xs), min(ys), zoom)
    east, south = tile_to_lonlat(max(xs) + 1, max(ys) + 1, zoom)
    return west, south, east, north


# --- Mosaic -----------------------------------------------------------------


def _geotransform(x: int, y: int, zoom: int, tile_px: int) -> str:
    """Return the GDAL geotransform of a raster whose top-left tile is (x, y)."""
    size = tile_size_m(zoom)
    pixel = size / tile_px
    west = -MERCATOR_HALF + x * size
    north = MERCATOR_HALF - y * size
    return f"{west!r}, {pixel!r}, 0.0, {north!r}, 0.0, {-pixel!r}"


def block_vrt_xml(
    raw_name: str, block_x: int, block_y: int, zoom: int, block_tiles: int, tile_px: int
) -> str:
    """Return the VRT describing one raw Float32 block as a georeferenced raster.

    A VRTRawRasterBand: GDAL reads the file as a plain little-endian array, row
    after row, with the SRS and geotransform stated here.
    """
    side = block_tiles * tile_px
    transform = _geotransform(
        block_x * block_tiles, block_y * block_tiles, zoom, tile_px
    )
    return (
        f'<VRTDataset rasterXSize="{side}" rasterYSize="{side}">\n'
        "  <SRS>EPSG:3857</SRS>\n"
        f"  <GeoTransform>{transform}</GeoTransform>\n"
        '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTRawRasterBand">\n'
        f"    <NoDataValue>{NODATA_M!r}</NoDataValue>\n"
        f'    <SourceFilename relativeToVRT="1">{escape(raw_name)}</SourceFilename>\n'
        "    <ImageOffset>0</ImageOffset>\n"
        "    <PixelOffset>4</PixelOffset>\n"
        f"    <LineOffset>{side * 4}</LineOffset>\n"
        "    <ByteOrder>LSB</ByteOrder>\n"
        "  </VRTRasterBand>\n"
        "</VRTDataset>\n"
    )


def mosaic_vrt_xml(
    blocks: Sequence[tuple[int, int, str]], zoom: int, block_tiles: int, tile_px: int
) -> str:
    """Return the VRT laying every block out on one EPSG:3857 raster.

    ``blocks`` are (block_x, block_y, block VRT path relative to the mosaic).
    Ground no block covers reads as the band's NoDataValue, which GDAL fills in
    for any part of a VRT no source writes.
    """
    if not blocks:
        raise ValueError("no blocks to mosaic")
    bx0 = min(b[0] for b in blocks)
    by0 = min(b[1] for b in blocks)
    bx1 = max(b[0] for b in blocks)
    by1 = max(b[1] for b in blocks)
    side = block_tiles * tile_px
    width = (bx1 - bx0 + 1) * side
    height = (by1 - by0 + 1) * side
    transform = _geotransform(bx0 * block_tiles, by0 * block_tiles, zoom, tile_px)
    lines = [
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
        "  <SRS>EPSG:3857</SRS>",
        f"  <GeoTransform>{transform}</GeoTransform>",
        '  <VRTRasterBand dataType="Float32" band="1">',
        f"    <NoDataValue>{NODATA_M!r}</NoDataValue>",
    ]
    for block_x, block_y, name in sorted(blocks):
        lines += [
            "    <SimpleSource>",
            f'      <SourceFilename relativeToVRT="1">{escape(name)}</SourceFilename>',
            "      <SourceBand>1</SourceBand>",
            f'      <SourceProperties RasterXSize="{side}" RasterYSize="{side}" '
            f'DataType="Float32" BlockXSize="{side}" BlockYSize="1"/>',
            f'      <SrcRect xOff="0" yOff="0" xSize="{side}" ySize="{side}"/>',
            f'      <DstRect xOff="{(block_x - bx0) * side}" '
            f'yOff="{(block_y - by0) * side}" xSize="{side}" ySize="{side}"/>',
            "    </SimpleSource>",
        ]
    lines += ["  </VRTRasterBand>", "</VRTDataset>", ""]
    return "\n".join(lines)


def tile_path(tile_dir: Path, zoom: int, x: int, y: int) -> Path:
    """Return where the build's fetch stores one tile: ``<dir>/<z>/<x>/<y>.webp``."""
    return tile_dir / str(zoom) / str(x) / f"{y}.webp"


def decode_tiles(
    tiles: Iterable[Tile],
    tile_dir: Path,
    out: Path,
    zoom: int = ZOOM,
    block_tiles: int = BLOCK_TILES,
    tile_px: int = TILE_PIXELS,
) -> dict[str, Any]:
    """Decode fetched tiles into Float32 blocks and write ``mosaic.vrt`` over them.

    Each block that holds at least one listed tile is written whole, with
    no-data wherever a tile is missing or was not listed. A block whose listed
    tiles all came back empty is not written at all. Returns a summary.
    """
    by_block: dict[Tile, list[Tile]] = {}
    for x, y in tiles:
        by_block.setdefault((x // block_tiles, y // block_tiles), []).append((x, y))

    out.mkdir(parents=True, exist_ok=True)
    side = block_tiles * tile_px
    written: list[tuple[int, int, str]] = []
    decoded = missing = 0
    for (block_x, block_y), members in sorted(by_block.items()):
        raw_name = f"{block_x}-{block_y}.f32"
        vrt_name = f"{block_x}-{block_y}.vrt"
        block = np.full((side, side), NODATA_M, dtype="<f4")
        found = False
        for x, y in members:
            heights = read_tile(tile_path(tile_dir, zoom, x, y), tile_px)
            if heights is None:
                missing += 1
                continue
            row = (y - block_y * block_tiles) * tile_px
            col = (x - block_x * block_tiles) * tile_px
            block[row : row + tile_px, col : col + tile_px] = heights
            decoded += 1
            found = True
        if not found:
            continue
        block.tofile(out / raw_name)
        (out / vrt_name).write_text(
            block_vrt_xml(raw_name, block_x, block_y, zoom, block_tiles, tile_px),
            encoding="utf-8",
        )
        written.append((block_x, block_y, vrt_name))

    if not written:
        raise RuntimeError(
            f"none of the listed tiles under {tile_dir} held any heights — check "
            "the fetch reached the elevation copy"
        )
    mosaic = out / "mosaic.vrt"
    mosaic.write_text(
        mosaic_vrt_xml(written, zoom, block_tiles, tile_px), encoding="utf-8"
    )
    return {
        "vrt": str(mosaic),
        "blocks": len(written),
        "tiles_decoded": decoded,
        "tiles_missing": missing,
    }


# --- CLI --------------------------------------------------------------------


def read_tile_list(path: Path) -> list[Tile]:
    """Read ``z/x/y`` lines, as ``list`` writes them, into (x, y) tiles."""
    tiles = []
    for line in path.read_text(encoding="utf-8").split():
        _, x, y = line.split("/")
        tiles.append((int(x), int(y)))
    return tiles


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — list or decode the Terrarium tiles."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    lister = sub.add_parser("list", help="z/x/y of every tile the area needs")
    lister.add_argument("area", type=Path, help="supported-area GeoJSON")
    lister.add_argument("--zoom", type=int, default=ZOOM)
    lister.add_argument(
        "--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), default=None
    )
    lister.add_argument(
        "--print-bbox",
        action="store_true",
        help="print the listed tiles' WGS84 bbox instead of the tiles",
    )

    decoder = sub.add_parser("decode", help="decode fetched tiles into a VRT")
    decoder.add_argument("--tiles", type=Path, required=True, help="list output")
    decoder.add_argument("--tile-dir", type=Path, required=True)
    decoder.add_argument("--out", type=Path, required=True)
    decoder.add_argument("--zoom", type=int, default=ZOOM)
    decoder.add_argument("--block-tiles", type=int, default=BLOCK_TILES)

    args = parser.parse_args(argv)
    if args.command == "list":
        area = json.loads(args.area.read_text(encoding="utf-8"))
        bbox = tuple(args.bbox) if args.bbox else None
        tiles = tiles_for_area(area, args.zoom, bbox)
        if not tiles:
            print("error: the area and the box share no tiles", file=sys.stderr)
            return 1
        if args.print_bbox:
            print(*tiles_bbox(tiles, args.zoom))
        else:
            sys.stdout.write("".join(f"{args.zoom}/{x}/{y}\n" for x, y in tiles))
        return 0

    summary = decode_tiles(
        read_tile_list(args.tiles),
        args.tile_dir,
        args.out,
        zoom=args.zoom,
        block_tiles=args.block_tiles,
    )
    print(
        f"==> {summary['tiles_decoded']} tiles decoded into {summary['blocks']} "
        f"blocks ({summary['tiles_missing']} absent) -> {summary['vrt']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
