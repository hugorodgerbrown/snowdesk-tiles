#!/usr/bin/env python3
r"""Blend swissALTI3D into the Mapterhorn fill along its edge (SNOW-1505).

The composite takes swissALTI3D wherever it has a height and the national model
under it everywhere else, so where the two disagree the grid steps at
swissALTI3D's edge. On stable ground they agree to a metre or two (the datum
check measures it). On a glacier they need not: the first Alps-wide build found
a 21 m step at Theodul, where swissALTI3D stops 3 km into Italy and the Italian
model, surveyed years earlier on thicker ice, carries on. A 21 m step between
two 5 m cells reads as a cliff, and the steep-terrain classes would draw it as a
line of extreme slope along the edge.

So the last ``BLEND_CELLS`` cells of swissALTI3D before its edge — 250 m — are
ramped from the national model's height at the edge to swissALTI3D's own:

    height = w * swiss + (1 - w) * fill,    w = k / BLEND_CELLS

where ``k`` is the number of 8-neighbour steps from the nearest cell the fill
covers and swissALTI3D does not. A 21 m step becomes a 21 m ramp over 250 m,
about 5 degrees. Cells further in, and cells with no fill under them, keep
swissALTI3D's height exactly. Hugo chose this on 20 Nov 2026.

It edits the quantised grid in place, inside a window round swissALTI3D's
coverage, reading two Int16 rasters ``build-terrain.sh`` cuts out of the warps
with the same ``-scale`` as grid.raw:

    python scripts/blend_seam.py window \
        --manifest work/terrain/swissalti3d-manifest.json \
        --west W --north N --cols C --rows R
    python scripts/blend_seam.py blend --raw work/terrain/grid.raw --cols C --rows R \
        --swiss swiss-window.raw --fill fill-window.raw \
        --xoff X --yoff Y --width WIDTH --height HEIGHT

The result depends only on the two window rasters, never on what grid.raw held,
so running it twice writes the same heights.

Requires numpy.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from terrain_grid import CELL_SIZE_M, NODATA, lonlat_extent, snap_extent

logger = logging.getLogger(__name__)

#: The ramp's width in cells: 50 x 5 m = 250 m.
BLEND_CELLS = 50
#: Cells per side of the square blocks the window is worked in.
BLOCK_CELLS = 2048

Grid = NDArray[np.int16]


def _dilate(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """Return ``mask`` grown by one cell to all eight neighbours."""
    grown = mask.copy()
    grown[1:, :] |= mask[:-1, :]
    grown[:-1, :] |= mask[1:, :]
    rows = grown.copy()
    grown[:, 1:] |= rows[:, :-1]
    grown[:, :-1] |= rows[:, 1:]
    return grown


def blend_block(swiss: Grid, fill: Grid, cells: int = BLEND_CELLS) -> Grid:
    """Return the blended heights for one block; the caller keeps its core.

    ``swiss`` and ``fill`` are the same block of the two sources, NODATA where
    a source has no height. Distances are counted inside the block only, so a
    block needs a halo of ``cells`` round the part of it the caller keeps.
    """
    has_swiss = swiss != NODATA
    has_fill = fill != NODATA
    out = swiss.copy()
    frontier = ~has_swiss & has_fill
    if not frontier.any():
        return out
    steps = np.full(swiss.shape, cells + 1, dtype=np.int16)
    reached = frontier.copy()
    for step in range(1, cells + 1):
        frontier = _dilate(frontier) & has_swiss & ~reached
        if not frontier.any():
            break
        steps[frontier] = step
        reached |= frontier
    ramp = has_swiss & has_fill & (steps <= cells)
    weight = steps[ramp].astype(np.float64) / cells
    mixed = weight * swiss[ramp] + (1.0 - weight) * fill[ramp]
    out[ramp] = np.rint(mixed).astype(np.int16)
    return out


def blend_window(
    raw: Grid,
    swiss: Grid,
    fill: Grid,
    xoff: int,
    yoff: int,
    cells: int = BLEND_CELLS,
    block: int = BLOCK_CELLS,
) -> int:
    """Write the blend into ``raw`` over the window; return the cells changed.

    ``swiss`` and ``fill`` are the window, ``raw`` the whole grid with the
    window's top-left at (``yoff``, ``xoff``). Only cells swissALTI3D covers can
    change. Blocks with no edge in reach are skipped untouched.
    """
    height, width = swiss.shape
    changed = 0
    for top in range(0, height, block):
        for left in range(0, width, block):
            r0, c0 = max(top - cells, 0), max(left - cells, 0)
            r1 = min(top + block + cells, height)
            c1 = min(left + block + cells, width)
            s = np.asarray(swiss[r0:r1, c0:c1])
            f = np.asarray(fill[r0:r1, c0:c1])
            has_swiss = s != NODATA
            if not has_swiss.any() or has_swiss.all():
                continue
            blended = blend_block(s, f, cells)
            core = (
                slice(top - r0, min(top + block, height) - r0),
                slice(left - c0, min(left + block, width) - c0),
            )
            new = blended[core]
            target = raw[
                yoff + top : yoff + top + new.shape[0],
                xoff + left : xoff + left + new.shape[1],
            ]
            core_swiss = has_swiss[core]
            diff = core_swiss & (target != new)
            count = int(diff.sum())
            if count:
                target[diff] = new[diff]
                changed += count
        logger.info("rows %d-%d of %d blended", top, min(top + block, height), height)
    return changed


def window(
    bbox: list[float], west: int, north: int, cols: int, rows: int
) -> tuple[int, int, int, int]:
    """Return the pixel window (xoff, yoff, width, height) round a WGS84 bbox.

    The bbox's tile-aligned extent plus one tile on every side, clipped to the
    grid, so the ramp has room on every side of swissALTI3D's coverage.
    """
    west_deg, south_deg, east_deg, north_deg = bbox
    extent = snap_extent(
        *lonlat_extent(west_deg, south_deg, east_deg, north_deg), margin_tiles=1
    )
    x0 = max((extent.west - west) // CELL_SIZE_M, 0)
    y0 = max((north - extent.north) // CELL_SIZE_M, 0)
    x1 = min((extent.east - west) // CELL_SIZE_M, cols)
    y1 = min((north - extent.south) // CELL_SIZE_M, rows)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"swissALTI3D's bbox {bbox} lies outside the grid")
    return int(x0), int(y0), int(x1 - x0), int(y1 - y0)


def _cmd_window(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    bbox = [float(value) for value in manifest["bbox"]]
    print(*window(bbox, args.west, args.north, args.cols, args.rows))
    return 0


def _cmd_blend(args: argparse.Namespace) -> int:
    expected = args.width * args.height * 2
    for path in (args.swiss, args.fill):
        size = path.stat().st_size
        if size != expected:
            raise ValueError(f"{path} is {size} bytes, expected {expected}")
    shape = (args.height, args.width)
    swiss = np.memmap(args.swiss, dtype="<i2", mode="r", shape=shape)
    fill = np.memmap(args.fill, dtype="<i2", mode="r", shape=shape)
    raw = np.memmap(args.raw, dtype="<i2", mode="r+", shape=(args.rows, args.cols))
    changed = blend_window(raw, swiss, fill, args.xoff, args.yoff)
    raw.flush()
    print(f"==> {changed} cells along swissALTI3D's edge blended into the fill")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_window = sub.add_parser("window", help="the pixel window to cut")
    p_window.add_argument("--manifest", type=Path, required=True)
    for name in ("west", "north", "cols", "rows"):
        p_window.add_argument(f"--{name}", type=int, required=True)
    p_window.set_defaults(func=_cmd_window)

    p_blend = sub.add_parser("blend", help="blend the edge into grid.raw")
    for name in ("raw", "swiss", "fill"):
        p_blend.add_argument(f"--{name}", type=Path, required=True)
    for name in ("cols", "rows", "xoff", "yoff", "width", "height"):
        p_blend.add_argument(f"--{name}", type=int, required=True)
    p_blend.set_defaults(func=_cmd_blend)

    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    sys.exit(main())
