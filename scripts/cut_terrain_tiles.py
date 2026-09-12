#!/usr/bin/env python3
r"""Cut a warped elevation raster into skirted Int16 terrain tiles.

SNOW-908. The GDAL half of the build (``build-terrain.sh``) ends with one flat
little-endian Int16 raster covering the whole grid, already reprojected to
EPSG:3035, already resampled to 5 m, and already quantised onto the stored
scale. This script turns that into the objects we publish: one file per tile,
each holding its 256x256 data cells plus a one-cell skirt, and a ``grid.json``
describing the lot.

Because the raster arrives pre-quantised and pre-aligned, cutting is pure byte
slicing — no arithmetic per cell, no numpy, no GDAL. That is the whole reason
the pipeline is arranged this way round. The alternative, reading floats and
converting them here, would be three billion Python-level operations and would
have needed a binary dependency in a repo that has none.

The skirt is what makes a tile self-sufficient. A slope at a coordinate is read
from the cell and its neighbours, so a sample landing in the outermost data cell
of a tile needs the first cell of the next tile along. Storing that one-cell
overlap costs 1.6% of the bytes and saves SNOW-917 from fetching up to four
tiles to answer one question near a boundary.

A tile whose data cells are all nodata is not written. Switzerland is a diagonal
country in a rectangular grid, so roughly half the tiles in the extent hold no
ground at all; the Worker answers 204 for anything absent, which is both the
cheap answer and the honest one.

Tiles are stored uncompressed. Int16 terrain would gzip to about half, but the
saving is pennies a month of R2 storage, and a stored encoding is a thing
SNOW-917 has to agree with — the fewer clauses in that contract the better.

Standalone operational tooling: stdlib only.

Usage:
    python scripts/cut_terrain_tiles.py \
        --raster work/grid.raw --west 4004480 --north 2754560 \
        --cols 71680 --rows 47360 --out dist/terrain \
        --origin https://tiles.snowdesk-data.info --version v1
"""

from __future__ import annotations

import argparse
import json
import mmap
import struct
import sys
from pathlib import Path
from typing import Any

from terrain_grid import (
    CELL_SIZE_M,
    NODATA,
    SKIRT_CELLS,
    SOURCES,
    STORED_CELLS,
    TILE_BYTES,
    TILE_CELLS,
    TILE_SIZE_M,
    definition,
)

NODATA_CELL = struct.pack("<h", NODATA)
NODATA_DATA = NODATA_CELL * TILE_CELLS


class Raster:
    """A flat, north-up, tile-aligned Int16 raster on disk."""

    def __init__(self, path: Path, west: int, north: int, cols: int, rows: int):
        """Open ``path`` and check it is the shape and alignment claimed."""
        if west % TILE_SIZE_M or north % TILE_SIZE_M:
            raise ValueError(
                f"raster corner ({west}, {north}) is not on a {TILE_SIZE_M} m "
                "tile boundary — the warp extent must come from "
                "`terrain_grid.py extent`"
            )
        if cols % TILE_CELLS or rows % TILE_CELLS:
            raise ValueError(
                f"raster is {cols}x{rows} cells, not a whole number of "
                f"{TILE_CELLS}-cell tiles"
            )
        expected = cols * rows * 2
        actual = path.stat().st_size
        if actual != expected:
            raise ValueError(
                f"{path} is {actual} bytes; {cols}x{rows} Int16 cells is "
                f"{expected}. Wrong -te, wrong -tr, or a truncated warp."
            )
        self.west, self.north, self.cols, self.rows = west, north, cols, rows
        self._handle = path.open("rb")
        self._map = mmap.mmap(self._handle.fileno(), 0, access=mmap.ACCESS_READ)

    def close(self) -> None:
        """Release the mapping and the file handle."""
        self._map.close()
        self._handle.close()

    @property
    def tiles_x(self) -> int:
        """Number of tile columns the raster spans."""
        return self.cols // TILE_CELLS

    @property
    def tiles_y(self) -> int:
        """Number of tile rows the raster spans."""
        return self.rows // TILE_CELLS

    @property
    def tile_x0(self) -> int:
        """Index of the westernmost tile column."""
        return self.west // TILE_SIZE_M

    @property
    def tile_y0(self) -> int:
        """Index of the southernmost tile row."""
        return self.north // TILE_SIZE_M - self.tiles_y

    def row_bytes(self, row: int, col: int, count: int) -> bytes:
        """Return ``count`` cells from a raster row, padded where out of range.

        Windows are allowed to hang off the edge: the outermost tiles of the
        extent have skirts that do, and a skirt over nothing is nodata rather
        than an error.
        """
        if row < 0 or row >= self.rows:
            return NODATA_CELL * count
        low = max(col, 0)
        high = min(col + count, self.cols)
        if high <= low:
            return NODATA_CELL * count
        start = (row * self.cols + low) * 2
        return (
            NODATA_CELL * (low - col)
            + self._map[start : start + (high - low) * 2]
            + NODATA_CELL * (col + count - high)
        )

    def tile(self, tile_x: int, tile_y: int) -> bytes | None:
        """Return a tile's stored bytes, or None if it holds no data at all."""
        # The stored window starts one cell north and west of the data area.
        row0 = (self.north - (tile_y + 1) * TILE_SIZE_M) // CELL_SIZE_M - SKIRT_CELLS
        col0 = (tile_x * TILE_SIZE_M - self.west) // CELL_SIZE_M - SKIRT_CELLS

        rows: list[bytes] = []
        has_data = False
        for offset in range(STORED_CELLS):
            row = self.row_bytes(row0 + offset, col0, STORED_CELLS)
            rows.append(row)
            # Only the data area decides whether a tile is worth storing. A tile
            # with nothing but skirt would be an empty answer wrapped in someone
            # else's edge.
            if not has_data and SKIRT_CELLS <= offset < SKIRT_CELLS + TILE_CELLS:
                data = row[SKIRT_CELLS * 2 : (SKIRT_CELLS + TILE_CELLS) * 2]
                has_data = data != NODATA_DATA
        if not has_data:
            return None
        return b"".join(rows)


def cut(raster: Raster, out: Path) -> dict[str, Any]:
    """Write every populated tile under ``out`` and summarise the coverage."""
    written = 0
    bounds: list[int] = []

    # North to south, west to east. Tiles in one row share raster rows, so this
    # order keeps the mapping reading roughly forwards through a file that is
    # larger than memory.
    for index_y in reversed(range(raster.tiles_y)):
        tile_y = raster.tile_y0 + index_y
        for index_x in range(raster.tiles_x):
            tile_x = raster.tile_x0 + index_x
            body = raster.tile(tile_x, tile_y)
            if body is None:
                continue
            if len(body) != TILE_BYTES:
                raise AssertionError(
                    f"tile {tile_x}/{tile_y} is {len(body)} bytes, not {TILE_BYTES}"
                )
            column = out / str(tile_x)
            column.mkdir(parents=True, exist_ok=True)
            (column / f"{tile_y}.s16").write_bytes(body)
            written += 1
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
            if written % 1000 == 0:
                print(f"    {written} tiles", end="\r", file=sys.stderr)

    if not written:
        raise RuntimeError(
            "no tile in the extent held any data — check the warp actually "
            "wrote heights, and that -dstnodata matches the stored nodata"
        )
    return {
        "tile_count": written,
        "tile_x": [bounds[0], bounds[2]],
        "tile_y": [bounds[1], bounds[3]],
        "bbox": [
            bounds[0] * TILE_SIZE_M,
            bounds[1] * TILE_SIZE_M,
            (bounds[2] + 1) * TILE_SIZE_M,
            (bounds[3] + 1) * TILE_SIZE_M,
        ],
    }


def grid_json(
    coverage: dict[str, Any],
    source: str,
    origin: str,
    version: str,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the published grid definition with this build's coverage in it."""
    if source not in SOURCES:
        raise ValueError(f"unknown terrain source {source!r}; add it to SOURCES")
    entry = dict(SOURCES[source]) | {"coverage": coverage}
    if manifest:
        entry["survey_years"] = manifest.get("survey_years", [])
        entry["source_tile_count"] = manifest.get("count")
    return definition(origin, version) | {"sources": [entry]}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — cut the raster and write the tiles and grid.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raster", required=True, help="flat Int16 raster to cut")
    parser.add_argument("--west", type=int, required=True, help="raster west edge (m)")
    parser.add_argument(
        "--north", type=int, required=True, help="raster north edge (m)"
    )
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--out", required=True, help="directory to write tiles into")
    parser.add_argument("--origin", default="", help="public origin serving the tiles")
    parser.add_argument("--version", default="", help="tile URL version segment")
    parser.add_argument(
        "--source",
        default="swissalti3d",
        choices=sorted(SOURCES),
        help="registry entry the heights came from",
    )
    parser.add_argument("--manifest", help="fetch manifest, for provenance")
    args = parser.parse_args(argv)

    raster = Raster(Path(args.raster), args.west, args.north, args.cols, args.rows)
    out = Path(args.out)
    try:
        print(
            f"==> cutting {raster.tiles_x}x{raster.tiles_y} tiles into {out}",
            file=sys.stderr,
        )
        coverage = cut(raster, out)
    finally:
        raster.close()

    manifest = (
        json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if args.manifest
        else None
    )
    document = grid_json(coverage, args.source, args.origin, args.version, manifest)
    (out / "grid.json").write_text(json.dumps(document, indent=2) + "\n", "utf-8")

    print(
        f"==> {coverage['tile_count']} tiles, "
        f"x {coverage['tile_x'][0]}-{coverage['tile_x'][1]}, "
        f"y {coverage['tile_y'][0]}-{coverage['tile_y'][1]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
