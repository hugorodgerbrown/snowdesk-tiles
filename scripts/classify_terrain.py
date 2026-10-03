#!/usr/bin/env python3
r"""Classify every cell of the elevation grid into the terrain-class planes.

SNOW-987. Reads the flat Int16 grid ``build-terrain.sh`` leaves in
``work/terrain/grid.raw`` and writes three Byte planes on the same EPSG:3035
grid — height high byte, height low byte, class byte — plus a VRT that stacks
them into one georeferenced RGB raster for ``gdalwarp`` to reproject.

**The input is grid.raw, not the warped GeoTIFF.** ``warped.tif`` is the same
ground in Float32, before quantisation onto the stored quarter-metre scale, and
it differs from the published heights by up to 0.125 m per cell — enough, across
a 10 m window, to move an angle over a band edge. ``grid.raw`` is byte for byte
what the published ``.s16`` tiles were cut from, so a class computed here is the
class ``sample_slope`` computes from those tiles. It also already carries a
one-tile margin of nodata round the data, so the kernel never runs off real
ground at the edge of the raster.

**Classify in EPSG:3035, then warp.** ``sample_slope`` differences cells on the
3035 grid. Computing slope after the warp would difference Mercator pixels
instead — different spacing, different axes, and a different answer that no
point sample would ever reproduce.

**Whole raster in row strips, no tile skirts.** The kernel reaches two cells in
every direction, so each strip is read with a two-row halo above and below and
the strips join without a seam. There are no tiles at this stage to have edges.

The arithmetic is numpy, operation for operation the scalar ``horn`` in
``terrain_class.py`` (itself a copy of sample_slope's), so the two agree to the
last bit on the additions and divisions. They need not agree on the last bit of
``arctan``, ``hypot`` and ``arctan2``, which numpy and CPython's ``math`` take
from different libraries, and on a cell whose angle or bearing lands within a
hair of a band or octant edge that last bit decides the class. Those cells are
rare — the heights are quarter-metres, so an angle exactly on an edge is
arithmetically impossible except at 0 and 45 degrees — and they are
recomputed with the scalar kernel, so the planes equal the scalar answer at
every cell rather than at almost every cell.

Operational tooling, not runtime: this is the one script in the repo that needs
numpy, which comes from the ``build`` extra or ``apt install python3-numpy``.

Usage:
    python scripts/classify_terrain.py \
        --raster work/terrain/grid.raw --west 4004480 --north 2754560 \
        --cols 71680 --rows 47360 --out work/terrain-class
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import BinaryIO

import numpy as np
from numpy.typing import NDArray
from terrain_class import (
    BAND_DEG,
    KERNEL_OFFSETS,
    LEVEL,
    MAX_BAND,
    NO_DATA,
    OCTANT_DEG,
    OCTANT_SHIFT,
    OCTANTS,
    WINDOW_SPACING_M,
    WINDOW_STEP_CELLS,
    classify_kernel,
)
from terrain_grid import CELL_SIZE_M, GRID_CRS, HEIGHT_SCALE_M, NODATA, TILE_SIZE_M

#: How close, in degrees, an angle or bearing has to be to a class edge before
#: the cell is handed to the scalar kernel. A double near 90 has a last bit of
#: about 1e-14 degrees, so this is several orders of magnitude of headroom for
#: two libm implementations to disagree in, and still catches almost no cells.
EDGE_TOLERANCE_DEG = 1e-9

#: Raster rows classified per strip. 64 rows of a 71,680-column grid is a few
#: hundred MB of float temporaries — comfortable on any build box.
STRIP_ROWS = 64

PLANES = ("r", "g", "b")

Planes = tuple[NDArray[np.uint8], NDArray[np.uint8], NDArray[np.uint8]]


def classify_block(block: NDArray[np.int16]) -> Planes:
    """Classify the interior rows of a block of stored heights.

    ``block`` is a north-up window of the grid with ``WINDOW_STEP_CELLS`` rows
    of halo above and below; the result covers the rows between the halos and
    every column. Cells within the step of the left or right edge have no
    complete kernel and are no data — at the raster's edge that is ground the
    margin guarantees is empty anyway.
    """
    step = WINDOW_STEP_CELLS
    height, width = block.shape
    out_rows = height - 2 * step
    if out_rows <= 0:
        raise ValueError(f"block of {height} rows has no rows inside its halo")

    r = np.zeros((out_rows, width), dtype=np.uint8)
    g = np.zeros((out_rows, width), dtype=np.uint8)
    b = np.full((out_rows, width), NO_DATA, dtype=np.uint8)
    inner = width - 2 * step
    if inner <= 0:
        return r, g, b

    def view[T: np.generic](array: NDArray[T], row: int, col: int) -> NDArray[T]:
        top, left = step + row, step + col
        return array[top : top + out_rows, left : left + inner]

    stored = [view(block, row, col) for row, col in KERNEL_OFFSETS]
    void = np.zeros((out_rows, inner), dtype=bool)
    for cells in stored:
        void |= cells == NODATA

    # Metres exactly as sample_slope's _to_metres makes them: the stored value
    # times the scale, in double precision. Converted once for the whole block.
    metres = block.astype(np.float64) * HEIGHT_SCALE_M
    (
        north_west,
        north,
        north_east,
        west,
        _,
        east,
        south_west,
        south,
        south_east,
    ) = (view(metres, row, col) for row, col in KERNEL_OFFSETS)

    # The scalar horn's expressions verbatim, so the rounding of every addition
    # and division happens in the same order on both sides.
    east_gradient = (
        (north_east + 2 * east + south_east) - (north_west + 2 * west + south_west)
    ) / (8 * WINDOW_SPACING_M)
    south_gradient = (
        (south_west + 2 * south + south_east) - (north_west + 2 * north + north_east)
    ) / (8 * WINDOW_SPACING_M)

    angle = np.degrees(np.arctan(np.hypot(east_gradient, south_gradient)))
    level = (east_gradient == 0) & (south_gradient == 0)
    aspect = np.degrees(np.arctan2(-east_gradient, south_gradient)) % 360

    band = np.minimum(angle // BAND_DEG, MAX_BAND).astype(np.uint8)
    octant = (((aspect % 360.0) + OCTANT_DEG / 2) // OCTANT_DEG).astype(np.int64)
    octant %= len(OCTANTS)
    classes = (octant.astype(np.uint8) << OCTANT_SHIFT) | band
    classes[level] = LEVEL
    classes[void] = NO_DATA

    # Height of the centre cell, rounded half up — height_metres() in numpy.
    centre = view(metres, 0, 0)
    whole = np.floor(centre + 0.5).astype(np.int64)
    whole[void] = 0
    if (whole < 0).any():
        raise ValueError(
            "a cell is below 0 m — the stored scale has been misread, or a "
            "source below sea level needs a different height encoding"
        )

    inner_r = (whole >> 8).astype(np.uint8)
    inner_g = (whole & 0xFF).astype(np.uint8)

    # The handful of cells sitting on a class edge, where numpy's and CPython's
    # transcendental functions are allowed to round differently. Level cells
    # never need it: they are decided by exact arithmetic, not by atan.
    near_edge = ~void & ~level
    near_edge &= _near_multiple(angle, BAND_DEG) | _near_multiple(
        aspect + OCTANT_DEG / 2, OCTANT_DEG
    )
    for row, col in zip(*np.nonzero(near_edge), strict=True):
        kernel = [int(cells[row, col]) for cells in stored]
        _, _, scalar_b, _ = classify_kernel(kernel)
        classes[row, col] = scalar_b

    r[:, step : step + inner] = inner_r
    g[:, step : step + inner] = inner_g
    b[:, step : step + inner] = classes
    return r, g, b


def _near_multiple(values: NDArray[np.float64], unit: float) -> NDArray[np.bool_]:
    """Return where values lie within the edge tolerance of a multiple of unit."""
    offset = np.abs(values - np.round(values / unit) * unit)
    result: NDArray[np.bool_] = offset < EDGE_TOLERANCE_DEG
    return result


def read_grid(path: Path, cols: int, rows: int) -> NDArray[np.int16]:
    """Map the flat little-endian Int16 grid without reading it into memory."""
    expected = cols * rows * 2
    actual = path.stat().st_size
    if actual != expected:
        raise ValueError(
            f"{path} is {actual} bytes; {cols}x{rows} Int16 cells is {expected}"
        )
    grid: NDArray[np.int16] = np.memmap(path, dtype="<i2", mode="r", shape=(rows, cols))
    return grid


def classify_grid(
    grid: NDArray[np.int16],
    sinks: tuple[BinaryIO, BinaryIO, BinaryIO],
    strip_rows: int = STRIP_ROWS,
) -> int:
    """Classify a whole grid strip by strip, streaming each plane to a sink.

    Returns the number of cells that ended up with a class rather than no data,
    which is the figure that says whether the run did anything.
    """
    step = WINDOW_STEP_CELLS
    rows, cols = grid.shape
    classified = 0
    for start in range(0, rows, strip_rows):
        stop = min(start + strip_rows, rows)
        # The halo, padded with nodata where it hangs off the raster: a kernel
        # reaching past the edge has a hole in it, and a hole voids it.
        top, bottom = max(start - step, 0), min(stop + step, rows)
        block = np.full((stop - start + 2 * step, cols), NODATA, dtype=np.int16)
        offset = top - (start - step)
        block[offset : offset + bottom - top] = grid[top:bottom]
        planes = classify_block(block)
        for plane, sink in zip(planes, sinks, strict=True):
            sink.write(plane.tobytes())
        classified += int(np.count_nonzero(planes[2] != NO_DATA))
        print(f"    row {stop}/{rows}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return classified


def vrt(west: int, north: int, cols: int, rows: int) -> str:
    """Return a VRT stacking the three planes into one georeferenced raster.

    Raw bands rather than a GeoTIFF: the planes are written as flat bytes as
    they are computed, and a VRT pointing at them is the cheapest way to give
    gdalwarp a georeferenced view of them with no copy. No band declares a
    nodata value — every byte is data, 255 included — so gdalwarp never treats
    a class as a hole.
    """
    bands = []
    for index, (plane, interp) in enumerate(
        zip(PLANES, ("Red", "Green", "Blue"), strict=True), start=1
    ):
        bands.append(
            f'  <VRTRasterBand dataType="Byte" band="{index}" '
            f'subClass="VRTRawRasterBand">\n'
            f"    <ColorInterp>{interp}</ColorInterp>\n"
            f'    <SourceFilename relativetoVRT="1">class_{plane}.raw'
            f"</SourceFilename>\n"
            f"    <ImageOffset>0</ImageOffset>\n"
            f"    <PixelOffset>1</PixelOffset>\n"
            f"    <LineOffset>{cols}</LineOffset>\n"
            f"  </VRTRasterBand>\n"
        )
    return (
        f'<VRTDataset rasterXSize="{cols}" rasterYSize="{rows}">\n'
        f"  <SRS>{GRID_CRS}</SRS>\n"
        f"  <GeoTransform>{west}, {CELL_SIZE_M}, 0, {north}, 0, "
        f"-{CELL_SIZE_M}</GeoTransform>\n" + "".join(bands) + "</VRTDataset>\n"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — classify grid.raw into planes and a VRT."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raster", required=True, help="flat Int16 grid to read")
    parser.add_argument("--west", type=int, required=True, help="raster west edge (m)")
    parser.add_argument(
        "--north", type=int, required=True, help="raster north edge (m)"
    )
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--out", required=True, help="directory for planes and VRT")
    parser.add_argument("--strip-rows", type=int, default=STRIP_ROWS)
    args = parser.parse_args(argv)

    if args.west % TILE_SIZE_M or args.north % TILE_SIZE_M:
        parser.error(
            f"({args.west}, {args.north}) is not on a {TILE_SIZE_M} m tile "
            "boundary — pass the extent build-terrain.sh warped to"
        )

    grid = read_grid(Path(args.raster), args.cols, args.rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"==> classifying {args.cols}x{args.rows} cells into {out}", file=sys.stderr)
    paths = [out / f"class_{plane}.raw" for plane in PLANES]
    # Written under a temporary name and renamed at the end, so an interrupted
    # run never leaves planes that look finished to the resumable build.
    partial = [path.with_suffix(".raw.part") for path in paths]
    with (
        partial[0].open("wb") as red,
        partial[1].open("wb") as green,
        partial[2].open("wb") as blue,
    ):
        classified = classify_grid(grid, (red, green, blue), args.strip_rows)
    for temporary, final in zip(partial, paths, strict=True):
        temporary.replace(final)
    (out / "classes.vrt").write_text(
        vrt(args.west, args.north, args.cols, args.rows), encoding="utf-8"
    )

    if not classified:
        print("error: no cell received a class — is the grid empty?", file=sys.stderr)
        return 1
    print(f"==> {classified} cells classified", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
