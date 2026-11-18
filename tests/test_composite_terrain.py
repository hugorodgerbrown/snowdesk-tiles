"""Tests for compositing the two terrain sources (SNOW-1505). Need GDAL.

Two overlapping Float32 rasters on the grid: swissALTI3D over the west half,
Mapterhorn over all of it but a strip at the east end. The composite must hold
swissALTI3D's value wherever swissALTI3D has one, Mapterhorn's elsewhere, and
no data where neither does — and once quantised and cut, every tile a
swissALTI3D-only build writes must compare unchanged against it.

Skipped without gdalwarp: the composite is a GDAL command, and faking it
would test the fake. CI and the build box have GDAL; a laptop may not.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compare_grids import compare  # noqa: E402
from cut_terrain_tiles import Raster, cut  # noqa: E402
from terrain_grid import (  # noqa: E402
    CELL_SIZE_M,
    HEIGHT_MAX_M,
    HEIGHT_NODATA_M,
    NODATA,
    STORED_MAX,
    TILE_CELLS,
    TILE_SIZE_M,
)

pytestmark = pytest.mark.skipif(
    shutil.which("gdalwarp") is None or shutil.which("gdal_translate") is None,
    reason="GDAL (gdalwarp, gdal_translate) is not installed",
)

COMPOSITE = ROOT / "scripts" / "composite-terrain.sh"
COLS, ROWS = 2 * TILE_CELLS, TILE_CELLS
WEST, NORTH = 3238 * TILE_SIZE_M, 2006 * TILE_SIZE_M
EXTENT = (WEST, NORTH - ROWS * CELL_SIZE_M, WEST + COLS * CELL_SIZE_M, NORTH)
SWISS_UNTIL = TILE_CELLS + 40  # swissALTI3D reaches 40 cells into the east tile
EMPTY_FROM = COLS - 10  # neither source holds the last ten columns


def write_raster(path: Path, heights: np.ndarray) -> Path:
    """Write a Float32 array as a raw file and a georeferenced EPSG:3035 VRT."""
    raw = path.with_suffix(".f32")
    heights.astype("<f4").tofile(raw)
    path.write_text(
        f'<VRTDataset rasterXSize="{COLS}" rasterYSize="{ROWS}">\n'
        "  <SRS>EPSG:3035</SRS>\n"
        f"  <GeoTransform>{WEST}, {CELL_SIZE_M}, 0, {NORTH}, 0, {-CELL_SIZE_M}"
        "</GeoTransform>\n"
        '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTRawRasterBand">\n'
        f"    <NoDataValue>{HEIGHT_NODATA_M}</NoDataValue>\n"
        f'    <SourceFilename relativeToVRT="1">{raw.name}</SourceFilename>\n'
        "    <ImageOffset>0</ImageOffset><PixelOffset>4</PixelOffset>\n"
        f"    <LineOffset>{COLS * 4}</LineOffset><ByteOrder>LSB</ByteOrder>\n"
        "  </VRTRasterBand>\n"
        "</VRTDataset>\n",
        encoding="utf-8",
    )
    return path


def warp_to_tif(source: Path, out: Path) -> Path:
    """Stand in for the build's warps: the same grid, written as a GeoTIFF."""
    subprocess.run(  # noqa: S603 - fixed arguments, paths made here
        [  # noqa: S607 - gdal_translate from PATH, as the build runs it
            "gdal_translate",
            "-q",
            "-ot",
            "Float32",
            str(source),
            str(out),
        ],
        check=True,
    )
    return out


def read_float(path: Path, tmp: Path) -> np.ndarray:
    """Return a raster's band as a float array, through ENVI."""
    raw = tmp / f"{path.stem}.read.raw"
    subprocess.run(  # noqa: S603 - fixed arguments
        ["gdal_translate", "-q", "-of", "ENVI", str(path), str(raw)],  # noqa: S607
        check=True,
    )
    return np.fromfile(raw, dtype="<f4").reshape(ROWS, COLS)


def quantise_and_cut(source: Path, tmp: Path, name: str) -> Path:
    """Quantise as build-terrain.sh does, then cut; return the tile tree."""
    raw = tmp / f"{name}.raw"
    subprocess.run(  # noqa: S603 - fixed arguments
        [  # noqa: S607 - gdal_translate from PATH
            "gdal_translate",
            "-q",
            "-of",
            "ENVI",
            "-ot",
            "Int16",
            "-scale",
            str(HEIGHT_NODATA_M),
            str(HEIGHT_MAX_M),
            str(NODATA),
            str(STORED_MAX),
            "-a_nodata",
            str(NODATA),
            str(source),
            str(raw),
        ],
        check=True,
    )
    out = tmp / f"{name}-tiles"
    raster = Raster(raw, WEST, NORTH, COLS, ROWS)
    try:
        cut(raster, out)
    finally:
        raster.close()
    return out


@pytest.fixture
def built(tmp_path: Path) -> dict[str, Path]:
    cols = np.arange(COLS, dtype=np.float64)[None, :]
    rows = np.arange(ROWS, dtype=np.float64)[:, None]
    swiss = 1000.0 + 0.25 * cols + 0.5 * rows + np.zeros((ROWS, COLS))
    swiss[:, SWISS_UNTIL:] = HEIGHT_NODATA_M
    # Mapterhorn disagrees with swissALTI3D everywhere they overlap, so a cell
    # that took the wrong source shows.
    mapterhorn = np.full((ROWS, COLS), 700.0)
    mapterhorn[:, EMPTY_FROM:] = HEIGHT_NODATA_M

    swiss_tif = warp_to_tif(
        write_raster(tmp_path / "swiss.vrt", swiss), tmp_path / "warped.tif"
    )
    mapterhorn_tif = warp_to_tif(
        write_raster(tmp_path / "mapterhorn.vrt", mapterhorn),
        tmp_path / "mapterhorn.tif",
    )
    merged = tmp_path / "merged.tif"
    subprocess.run(  # noqa: S603 - the script under test, fixed arguments
        [
            str(COMPOSITE),
            str(mapterhorn_tif),
            str(swiss_tif),
            str(merged),
            *(str(value) for value in EXTENT),
        ],
        check=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHON": sys.executable},
    )
    return {"swiss": swiss_tif, "merged": merged}


def test_swissalti3d_wins_where_it_has_data(
    built: dict[str, Path], tmp_path: Path
) -> None:
    merged = read_float(built["merged"], tmp_path)
    swiss = read_float(built["swiss"], tmp_path)

    assert np.array_equal(merged[:, :SWISS_UNTIL], swiss[:, :SWISS_UNTIL])


def test_mapterhorn_fills_the_rest(built: dict[str, Path], tmp_path: Path) -> None:
    merged = read_float(built["merged"], tmp_path)

    assert np.all(merged[:, SWISS_UNTIL:EMPTY_FROM] == 700.0)


def test_no_data_stays_no_data(built: dict[str, Path], tmp_path: Path) -> None:
    merged = read_float(built["merged"], tmp_path)

    assert np.all(merged[:, EMPTY_FROM:] == HEIGHT_NODATA_M)


def test_cut_composite_keeps_every_swiss_height(
    built: dict[str, Path], tmp_path: Path
) -> None:
    # The pre-upload check end to end: a swissALTI3D-only build is the
    # reference, the composite the candidate, and compare_grids must pass.
    reference = quantise_and_cut(built["swiss"], tmp_path, "swiss")
    candidate = quantise_and_cut(built["merged"], tmp_path, "merged")

    assert compare(reference, candidate) == 2
