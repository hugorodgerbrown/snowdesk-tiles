#!/usr/bin/env python3
r"""Cut the warped class rasters into PNG map tiles, and write tiles.json.

SNOW-987. ``build-terrain-class.sh`` ends with one flat RGBA raster per zoom,
already in Web Mercator, already snapped to whole XYZ tiles: the three class
planes warped nearest-neighbour from EPSG:3035, plus the alpha band gdalwarp
adds to say which pixels any source cell landed on. This script slices each
into 256 px tiles, writes them as PNGs, and publishes a ``tiles.json``
descriptor carrying the pixel contract from ``terrain_class.py``.

**Alpha comes in as coverage and goes out as 255.** The warp's alpha is 0 where
no 3035 cell landed — outside the grid — and 255 everywhere else. The contract
has no such thing as a transparent pixel (a canvas premultiplies, and these
colours are data), so an uncovered pixel is rewritten to the no-data pixel,
``(0, 0, 255, 255)``, and every pixel leaves with alpha 255. The rewrite is a
single ``bytes.replace`` of four zero bytes per row: an uncovered pixel is all
zeros, a covered one ends in 0xFF, so no run of four zeros can straddle a
covered pixel and every match lands on a pixel boundary.

**PNG written by hand, filter 0, zlib.** Pillow would be a binary dependency in
a repo that has one optional one; a PNG is a signature, three chunks and a
zlib stream. Filter 0 (none) because the adaptive filters are a per-byte
Python loop over ~20,000 tiles, and the saving is a fraction of a GB that R2
bills at cents. The browser decodes any of them.

A tile with no data pixel at all — every pixel uncovered or voided — is not
written. The Worker answers 204 for an absent tile, which is both the cheap
answer and the honest one, exactly as for the elevation grid.

Standalone operational tooling: stdlib only.

Usage:
    python scripts/cut_terrain_class_tiles.py \
        --work work/terrain-class --manifest work/terrain/manifest.json \
        --out dist/terrain-class \
        --origin https://tiles.snowdesk-data.info --version v1
"""

from __future__ import annotations

import argparse
import json
import mmap
import shutil
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

from terrain_class import (
    ALPHA,
    MAX_ZOOM,
    MIN_ZOOM,
    NO_DATA,
    NODATA_PIXEL,
    PNG_SIGNATURE,
    TILE_PIXELS,
    MercatorExtent,
    descriptor,
    mercator_extent,
)

PIXEL_BYTES = 4
ROW_BYTES = TILE_PIXELS * PIXEL_BYTES
UNCOVERED = bytes(PIXEL_BYTES)
NODATA_BYTES = bytes(NODATA_PIXEL)
OPAQUE_ROW = bytes([ALPHA]) * TILE_PIXELS
NODATA_ROW = bytes([NO_DATA]) * TILE_PIXELS
TRANSPARENT_ROW = bytes(TILE_PIXELS)
ZERO_ROW = bytes(TILE_PIXELS)

#: zlib level. 6 is zlib's own default, and the knee of its curve: 9 is
#: markedly slower for a percent or two on tiles this noisy in the low byte.
COMPRESSION = 6


def raster_name(zoom: int) -> str:
    """Return the file name build-terrain-class.sh warps a zoom level into."""
    return f"mercator-z{zoom}.raw"


class MercatorRaster:
    """A flat, pixel-interleaved RGBA raster snapped to whole XYZ tiles."""

    def __init__(self, path: Path, extent: MercatorExtent):
        """Open ``path`` and check it is the size its extent says it is."""
        expected = extent.width * extent.height * PIXEL_BYTES
        actual = path.stat().st_size
        if actual != expected:
            raise ValueError(
                f"{path} is {actual} bytes; a {extent.width}x{extent.height} "
                f"RGBA raster is {expected}. Wrong -te or -ts, a missing "
                "-dstalpha, or a truncated warp."
            )
        self.extent = extent
        self._handle = path.open("rb")
        self._map = mmap.mmap(self._handle.fileno(), 0, access=mmap.ACCESS_READ)

    def close(self) -> None:
        """Release the mapping and the file handle."""
        self._map.close()
        self._handle.close()

    def tile(self, index_x: int, index_y: int) -> list[bytes] | None:
        """Return one tile's rows of RGBA pixels, or None if it holds no data.

        Indices are relative to the raster's own north-west tile.
        """
        width = self.extent.width
        rows: list[bytes] = []
        uncovered: list[bool] = []
        has_data = False
        for row in range(TILE_PIXELS):
            start = (index_y * TILE_PIXELS + row) * width + index_x * TILE_PIXELS
            start *= PIXEL_BYTES
            raw = self._map[start : start + ROW_BYTES]
            uncovered.append(raw[3::PIXEL_BYTES] == TRANSPARENT_ROW)
            line = raw.replace(UNCOVERED, NODATA_BYTES)
            if line[3::PIXEL_BYTES] != OPAQUE_ROW:
                line = uncover(line, index_x, index_y, row)
            check_nodata(line, index_x, index_y, row)
            if not has_data:
                has_data = line[2::PIXEL_BYTES] != NODATA_ROW
            rows.append(line)
        check_no_gap_rows(uncovered, index_x, index_y)
        return rows if has_data else None


def uncover(line: bytes, index_x: int, index_y: int, row: int) -> bytes:
    """Rewrite every alpha-0 pixel in one row to the no-data pixel.

    The fast path in ``MercatorRaster.tile`` only catches uncovered pixels the
    warper left as all zeros. GDAL does not always: on the z14 warp of the
    first live build it wrote source no-data cells as (0, 0, 255, 0), keeping
    the colour and clearing the alpha. Alpha 0 is the warper saying no source
    cell lies under the pixel, so the colour it left there means nothing.
    Walked pixel by pixel, which only rows at the edge of coverage pay for.
    """
    alphas = line[3::PIXEL_BYTES]
    pixels = bytearray(line)
    for index, alpha in enumerate(alphas):
        if alpha == ALPHA:
            continue
        if alpha != 0:
            # Partial alpha means something other than a nearest-neighbour
            # warp produced this raster, and the colour channels have been
            # blended with it — they are no longer classes.
            raise ValueError(
                f"tile ({index_x}, {index_y}) row {row} has an alpha other "
                "than 0 or 255 — was the warp not -r near?"
            )
        offset = index * PIXEL_BYTES
        pixels[offset : offset + PIXEL_BYTES] = NODATA_BYTES
    return bytes(pixels)


def check_nodata(line: bytes, index_x: int, index_y: int, row: int) -> None:
    """Refuse a no-data pixel that carries a height.

    The contract's no-data pixel is R = G = 0, and the classifier writes
    nothing else. A height under a no-data class means the bytes were not
    written by the classifier at all: the first live z14 raster held them,
    along with whole empty rows, and both went out to R2 before verify.sh
    caught them. A margin row of pure no data is checked in one comparison.
    """
    classes = line[2::PIXEL_BYTES]
    if NO_DATA not in classes:
        return
    if (
        classes == NODATA_ROW
        and line[0::PIXEL_BYTES] == ZERO_ROW
        and line[1::PIXEL_BYTES] == ZERO_ROW
    ):
        return
    index = classes.find(NO_DATA)
    while index != -1:
        offset = index * PIXEL_BYTES
        if line[offset] or line[offset + 1]:
            raise ValueError(
                f"tile ({index_x}, {index_y}) row {row} pixel {index} is no data "
                f"but carries a height ({line[offset]}, {line[offset + 1]}) — "
                "the warped raster is corrupt; re-warp this zoom"
            )
        index = classes.find(NO_DATA, index + 1)


def check_no_gap_rows(uncovered: list[bool], index_x: int, index_y: int) -> None:
    """Refuse a tile with a wholly uncovered row between covered rows.

    Coverage is the union of 1 km source squares, so its edge is never a
    single horizontal line a tile wide with ground on both sides. A row left
    empty between covered rows is a raster that was not wholly written, as
    the first live z14 raster was across Switzerland, and would show as
    no-data stripes on the map.
    """
    covered = [index for index, empty in enumerate(uncovered) if not empty]
    if not covered:
        return
    for row in range(covered[0], covered[-1] + 1):
        if uncovered[row]:
            raise ValueError(
                f"tile ({index_x}, {index_y}) row {row} is wholly uncovered "
                "between covered rows — the raster is incomplete; re-warp this zoom"
            )


def png(rows: list[bytes]) -> bytes:
    """Encode rows of RGBA pixels as an 8-bit, non-interlaced PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    width = len(rows[0]) // PIXEL_BYTES
    # Width, height, bit depth 8, colour type 6 (RGBA), compression method 0,
    # filter method 0, no interlace. Every row then declares filter type 0.
    header = struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, COMPRESSION))
        + chunk(b"IEND", b"")
    )


def cut(raster: MercatorRaster, out: Path) -> dict[str, Any]:
    """Write every populated tile of one zoom level and summarise them."""
    extent = raster.extent
    written = 0
    size = 0
    bounds: list[int] = []
    for index_y in range(extent.tiles_y):
        for index_x in range(extent.tiles_x):
            rows = raster.tile(index_x, index_y)
            if rows is None:
                continue
            tile_x, tile_y = extent.tile_x + index_x, extent.tile_y + index_y
            body = png(rows)
            column = out / str(extent.zoom) / str(tile_x)
            column.mkdir(parents=True, exist_ok=True)
            (column / f"{tile_y}.png").write_bytes(body)
            written += 1
            size += len(body)
            bounds = (
                [tile_x, tile_y, tile_x, tile_y]
                if not bounds
                else [
                    min(bounds[0], tile_x),
                    min(bounds[1], tile_y),
                    max(bounds[2], tile_x),
                    max(bounds[3], tile_y),
                ]
            )
            if written % 500 == 0:
                print(f"    z{extent.zoom}: {written} tiles", end="\r", file=sys.stderr)

    if not written:
        raise RuntimeError(
            f"no z{extent.zoom} tile held any data — check the warp wrote the "
            "class planes and an alpha band, over the extent the manifest names"
        )
    return {
        "zoom": extent.zoom,
        "tile_count": written,
        "bytes": size,
        "tile_x": [bounds[0], bounds[2]],
        "tile_y": [bounds[1], bounds[3]],
    }


def tiles_json(
    levels: list[dict[str, Any]],
    bbox: list[float],
    origin: str,
    version: str,
) -> dict[str, Any]:
    """Return the published descriptor with this build's coverage in it."""
    coverage = {
        "bounds": bbox,
        "tile_count": sum(level["tile_count"] for level in levels),
        "bytes": sum(level["bytes"] for level in levels),
        "levels": levels,
    }
    return descriptor(origin, version, coverage)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — cut every zoom and write the PNGs and tiles.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, help="directory of warped rasters")
    parser.add_argument("--manifest", required=True, help="terrain fetch manifest")
    parser.add_argument("--out", required=True, help="directory to write tiles into")
    parser.add_argument("--origin", default="", help="public origin serving tiles")
    parser.add_argument("--version", default="", help="tile URL version segment")
    parser.add_argument(
        "--zooms",
        type=int,
        nargs="+",
        default=list(range(MIN_ZOOM, MAX_ZOOM + 1)),
        help="zoom levels to cut",
    )
    args = parser.parse_args(argv)

    outside = [zoom for zoom in args.zooms if not MIN_ZOOM <= zoom <= MAX_ZOOM]
    if outside:
        parser.error(
            f"zoom {outside} is outside {MIN_ZOOM}-{MAX_ZOOM}; the Worker "
            "answers 204 there, so the tiles would never be served"
        )

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    west, south, east, north = (float(value) for value in manifest["bbox"])
    bbox = [west, south, east, north]
    out = Path(args.out)
    levels = []
    for zoom in args.zooms:
        # The extent is recomputed from the manifest rather than passed in, by
        # the same function that gave gdalwarp its -te and -ts — so the pixel
        # grid being sliced is the one that was warped.
        extent = mercator_extent(west, south, east, north, zoom)
        raster = MercatorRaster(Path(args.work) / raster_name(zoom), extent)
        # A rebuild into the same directory must not keep a tile this build
        # found empty: tiles.json would omit it, upload.sh would still publish
        # it, and the Worker would serve the old classes instead of a 204.
        shutil.rmtree(out / str(zoom), ignore_errors=True)
        try:
            print(
                f"==> z{zoom}: cutting {extent.tiles_x}x{extent.tiles_y} tiles",
                file=sys.stderr,
            )
            level = cut(raster, out)
        finally:
            raster.close()
        print(
            f"==> z{zoom}: {level['tile_count']} tiles, {level['bytes']:,} bytes",
            file=sys.stderr,
        )
        levels.append(level)

    document = tiles_json(levels, bbox, args.origin, args.version)
    # The descriptor promises z{MIN_ZOOM}-{MAX_ZOOM}, and so does the Worker,
    # so only a build of every level may carry one. A narrowed build (a quick
    # look at one zoom) still writes its tiles, but no tiles.json — and
    # upload.sh refuses a class directory without one, because it syncs with
    # --delete and would otherwise remove the levels this build skipped.
    descriptor_path = out / "tiles.json"
    if sorted(args.zooms) == list(range(MIN_ZOOM, MAX_ZOOM + 1)):
        descriptor_path.write_text(json.dumps(document, indent=2) + "\n", "utf-8")
    else:
        descriptor_path.unlink(missing_ok=True)
        print(
            f"==> partial build (z{sorted(args.zooms)}): no tiles.json written, "
            "so upload.sh will not publish it",
            file=sys.stderr,
        )
    print(
        f"==> {document['tile_count']} tiles, {document['bytes']:,} bytes "
        f"({document['bytes'] / 1e9:.2f} GB, the unit R2 bills in)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
