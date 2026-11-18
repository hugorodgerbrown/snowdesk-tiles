#!/usr/bin/env python3
r"""Stop the terrain build if a national model's heights disagree with swisstopo's.

SNOW-1505, stage 0 of build-terrain.sh, before anything is downloaded in bulk.
Mapterhorn does no vertical transformation: each national model keeps its own
height reference, and its catalogue does not record which. The grid puts
swissALTI3D (LN02) beside those models at every border, so a model on a
different reference would show as a step along the border, everywhere.

**The threshold is 1.0 m, and why.** The Alpine national datums — LN02,
NGF-IGN69, the Austrian and Italian national heights, DHHN2016 — are all
orthometric, and differ from one another by decimetres. The failure worth
stopping for is a model on ellipsoidal heights, which would sit tens of metres
off (the geoid is ~50 m above the ellipsoid here). A metre lies well between
the two. The ground compared is flat — under 1 degree — so the natural rise
across the two 25 m strips (50 m at most) is under 0.9 m, and a true datum
step is not hidden in a slope.

**What is compared.** At each crossing in datum_points.json, a 300 m window is
warped from both sources onto the grid's 5 m EPSG:3035 cells exactly as the
build will warp them. The border is where swissALTI3D's data stops. Within
100 m of the crossing — a 200 m stretch of border — the Swiss strip is the
swissALTI3D cells within 25 m of the border, and the foreign strip the
national-model cells within 25 m of it on the other side, clipped to the
coverage polygons the build will clip to. Only cells with a slope under 1
degree count, each side needs 20 of them, and the mean heights must agree
within 1.0 m. Too few flat cells fails loudly: a check that could not look is
not a check that passed. There is no flag to skip it.

Only the few swissALTI3D squares and z14 tiles each window needs are fetched.
``assess`` and ``strips`` are pure numpy, and the tests drive them with
synthetic ground.

Usage:
    python scripts/datum_check.py --work work/terrain/datum \
        --dem-url https://tiles.snowdesk-data.info/dem/2026-12 \
        --clip work/terrain/mapterhorn/clip.geojson
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fetch_swissalti3d
import numpy as np
from numpy.typing import NDArray
from terrain_grid import CELL_SIZE_M, HEIGHT_NODATA_M, lonlat_to_grid
from terrarium import ZOOM, decode_tiles, tile_path, tiles_for_box

POINTS = Path(__file__).resolve().parent / "datum_points.json"

#: See the module docstring for each of these.
MAX_DIFFERENCE_M = 1.0
MAX_SLOPE_DEG = 1.0
MIN_FLAT_CELLS = 20
STRIP_M = 25.0
HALF_LINE_M = 100.0
WINDOW_HALF_M = 150.0

Grid = NDArray[np.float64]
Mask = NDArray[np.bool_]


@dataclass(frozen=True)
class DatumResult:
    """The comparison at one crossing."""

    swiss_cells: int
    foreign_cells: int
    swiss_mean_m: float | None
    foreign_mean_m: float | None
    ok: bool
    reason: str

    @property
    def difference_m(self) -> float | None:
        """Swiss mean minus foreign mean, where both were measured."""
        if self.swiss_mean_m is None or self.foreign_mean_m is None:
            return None
        return self.swiss_mean_m - self.foreign_mean_m


def slope_deg(heights: Grid, cell_m: float) -> Grid:
    """Return each cell's slope in degrees; NaN where a neighbour is missing."""
    if min(heights.shape) < 2:
        return np.full(heights.shape, np.nan)
    d_row, d_col = np.gradient(heights, cell_m)
    slope: Grid = np.degrees(np.arctan(np.hypot(d_row, d_col)))
    return slope


def assess(
    swiss: Grid,
    swiss_strip: Mask,
    foreign: Grid,
    foreign_strip: Mask,
    cell_m: float = CELL_SIZE_M,
) -> DatumResult:
    """Compare the flat cells of two strips. Pure; the core of the check.

    ``swiss`` and ``foreign`` are heights in metres on the same grid, NaN where
    a source has none; the masks say which cells are in each strip. Slope is
    taken from each source's own heights, so a cell is flat by its own model.
    """
    swiss_flat = swiss_strip & (slope_deg(swiss, cell_m) < MAX_SLOPE_DEG)
    foreign_flat = foreign_strip & (slope_deg(foreign, cell_m) < MAX_SLOPE_DEG)
    swiss_count = int(np.count_nonzero(swiss_flat))
    foreign_count = int(np.count_nonzero(foreign_flat))

    if swiss_count < MIN_FLAT_CELLS or foreign_count < MIN_FLAT_CELLS:
        reason = (
            f"too few flat cells to compare: {swiss_count} Swiss and "
            f"{foreign_count} foreign under {MAX_SLOPE_DEG:g} degree, "
            f"{MIN_FLAT_CELLS} needed on each side"
        )
        if not np.any(foreign_strip):
            reason += (
                " — the foreign side holds no national-model cells; the crossing "
                "may lie outside every national model's coverage"
            )
        return DatumResult(swiss_count, foreign_count, None, None, False, reason)

    swiss_mean = float(np.mean(swiss[swiss_flat]))
    foreign_mean = float(np.mean(foreign[foreign_flat]))
    difference = swiss_mean - foreign_mean
    ok = abs(difference) <= MAX_DIFFERENCE_M
    reason = (
        f"means differ by {difference:+.2f} m over {swiss_count} Swiss and "
        f"{foreign_count} foreign flat cells"
        + ("" if ok else f" — more than {MAX_DIFFERENCE_M:g} m")
    )
    return DatumResult(swiss_count, foreign_count, swiss_mean, foreign_mean, ok, reason)


def _dilate(mask: Mask, radius_cells: int) -> Mask:
    """Return the cells within ``radius_cells`` (Euclidean) of any true cell."""
    rows, cols = mask.shape
    padded = np.pad(mask, radius_cells)
    out = np.zeros_like(mask)
    for dr in range(-radius_cells, radius_cells + 1):
        for dc in range(-radius_cells, radius_cells + 1):
            if dr * dr + dc * dc > radius_cells * radius_cells:
                continue
            out |= padded[
                radius_cells + dr : radius_cells + dr + rows,
                radius_cells + dc : radius_cells + dc + cols,
            ]
    return out


def strips(
    swiss: Grid,
    foreign: Grid,
    cell_m: float = CELL_SIZE_M,
    strip_m: float = STRIP_M,
    half_line_m: float = HALF_LINE_M,
) -> tuple[Mask, Mask]:
    """Return the Swiss and foreign strip masks of a window centred on a crossing.

    The border is where swissALTI3D's data stops: the Swiss strip is Swiss cells
    within ``strip_m`` of a cell Swiss data does not reach, and the foreign
    strip is foreign cells Swiss data does not reach within ``strip_m`` of one
    it does. Both are kept to within ``half_line_m`` of the window's centre.
    """
    swiss_valid = ~np.isnan(swiss)
    foreign_only = ~swiss_valid & ~np.isnan(foreign)
    radius = round(strip_m / cell_m)
    rows, cols = swiss.shape
    r, c = np.mgrid[0:rows, 0:cols]
    near = np.hypot((r - (rows - 1) / 2) * cell_m, (c - (cols - 1) / 2) * cell_m)
    near_centre = near <= half_line_m
    swiss_strip = swiss_valid & _dilate(~swiss_valid, radius) & near_centre
    foreign_strip = foreign_only & _dilate(swiss_valid, radius) & near_centre
    return swiss_strip, foreign_strip


# --- Fetching and warping one window -------------------------------------------


def window(lon: float, lat: float) -> tuple[float, float, float, float]:
    """Return a cell-aligned (west, south, east, north) window round a point."""
    easting, northing = lonlat_to_grid(lon, lat)
    west = math.floor((easting - WINDOW_HALF_M) / CELL_SIZE_M) * CELL_SIZE_M
    south = math.floor((northing - WINDOW_HALF_M) / CELL_SIZE_M) * CELL_SIZE_M
    side = 2 * WINDOW_HALF_M
    return west, south, west + side, south + side


def _download(url: str, dest: Path) -> bool:
    """Fetch ``url`` to ``dest``; False for a 204 (written as an empty file)."""
    if dest.exists():
        return dest.stat().st_size > 0
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - checked
        body = b"" if response.status == 204 else response.read()
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(body)
    part.rename(dest)
    return bool(body)


def _warp(
    sources: list[str], te: tuple[float, ...], out: Path, clip: Path | None
) -> Grid:
    """Gdalwarp sources onto the window's 5 m cells and return them, NaN for none."""
    cells = round((te[2] - te[0]) / CELL_SIZE_M)
    command = [
        "gdalwarp",
        "-q",
        "-overwrite",
        "-t_srs",
        "EPSG:3035",
        "-te",
        *(str(v) for v in te),
        "-tr",
        str(CELL_SIZE_M),
        str(CELL_SIZE_M),
        "-r",
        "average",
        "-ot",
        "Float32",
        "-dstnodata",
        str(HEIGHT_NODATA_M),
        "-of",
        "ENVI",
    ]
    if clip is not None:
        command += ["-cutline", str(clip)]
    # A fixed argument list, no shell: the inputs are paths this script made.
    subprocess.run([*command, *sources, str(out)], check=True)  # noqa: S603
    heights = np.fromfile(out, dtype="<f4").astype(np.float64).reshape(cells, cells)
    heights[heights == HEIGHT_NODATA_M] = np.nan
    return heights


def crossing_box(lon: float, lat: float) -> tuple[float, float, float, float]:
    """Return the (west, south, east, north) box fetched round a crossing.

    About 900 m a side: it holds the 200 m line and its strips with room for
    the border to sit anywhere the search allows.
    """
    return (lon - 0.006, lat - 0.004, lon + 0.006, lat + 0.004)


def check_crossing(
    crossing: dict[str, Any], work: Path, dem_url: str, clip: Path, gsd: float
) -> DatumResult:
    """Fetch, warp and assess one crossing."""
    name = str(crossing["name"])
    lon, lat = float(crossing["lon"]), float(crossing["lat"])
    here = work / "".join(ch if ch.isalnum() else "-" for ch in name)
    te = window(lon, lat)
    box = crossing_box(lon, lat)

    chosen = fetch_swissalti3d.select(fetch_swissalti3d.items(box), gsd)
    tifs = []
    for item in chosen:
        dest = here / "swiss" / Path(item["_href"]).name
        if _download(item["_href"], dest):
            tifs.append(str(dest))
    if not tifs:
        raise RuntimeError(f"{name}: no swissALTI3D squares near the crossing")

    # Every tile the window touches: the window is smaller than a z14 tile, so
    # a centre-in-area test can find none.
    tiles = tiles_for_box(box, ZOOM)
    for x, y in tiles:
        _download(
            f"{dem_url.rstrip('/')}/{ZOOM}/{x}/{y}.webp",
            tile_path(here / "webp", ZOOM, x, y),
        )
    mosaic = decode_tiles(tiles, here / "webp", here / "blocks")["vrt"]

    swiss = _warp(tifs, te, here / "swiss.raw", None)
    foreign = _warp([mosaic], te, here / "mapterhorn.raw", clip)
    swiss_strip, foreign_strip = strips(swiss, foreign)
    return assess(swiss, swiss_strip, foreign, foreign_strip)


def load_points(path: Path = POINTS) -> list[dict[str, Any]]:
    """Return the crossings in datum_points.json."""
    crossings: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))[
        "crossings"
    ]
    return crossings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — check every crossing; exit 1 if any fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--points", type=Path, default=POINTS)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--dem-url", required=True, help="dem/<version> base URL")
    parser.add_argument("--clip", type=Path, required=True, help="the build's clip")
    parser.add_argument("--gsd", type=float, default=2.0)
    args = parser.parse_args(argv)

    if shutil.which("gdalwarp") is None:
        print("error: gdalwarp not found — install GDAL", file=sys.stderr)
        return 1
    failed = 0
    for crossing in load_points(args.points):
        try:
            result = check_crossing(
                crossing, args.work, args.dem_url, args.clip, args.gsd
            )
        except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
            print(f"  FAIL  datum: {crossing['name']}: {error}")
            failed += 1
            continue
        status = "ok  " if result.ok else "FAIL"
        print(f"  {status}  datum: {crossing['name']}: {result.reason}")
        failed += 0 if result.ok else 1
    if failed:
        print(
            f"error: {failed} crossing(s) failed the datum check — the build stops "
            "before any bulk download",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
