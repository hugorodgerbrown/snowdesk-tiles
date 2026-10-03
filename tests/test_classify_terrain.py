"""Tests for classifying the elevation grid into class planes (SNOW-987).

The property that matters is equality with the scalar kernel at every cell, not
plausibility: a vectorised Horn that is a row out, a column mirrored or a halo
short still produces believable slopes everywhere, and the only thing that
catches it is comparing against ``classify_kernel`` — which is itself pinned to
sample_slope's literal answers in test_terrain_class.py.

The synthetic grid is built to put every branch under that comparison: planes
of known angle and facing, a cone whose flanks face all eight octants and cross
every band, a flat lake (level ground), a nodata hole in the middle of real
ground, and a coverage edge where the data simply stops.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from classify_terrain import (  # noqa: E402
    classify_block,
    classify_grid,
    main,
    read_grid,
    vrt,
)
from terrain_class import (  # noqa: E402
    KERNEL_OFFSETS,
    LEVEL,
    NO_DATA,
    NODATA_PIXEL,
    OCTANTS,
    WINDOW_STEP_CELLS,
    classify_kernel,
    decode_pixel,
)
from terrain_grid import NODATA, TILE_CELLS, TILE_SIZE_M  # noqa: E402

SIZE = 2 * TILE_CELLS


def synthetic_grid() -> NDArray[np.int16]:
    """Return a 512x512 stored grid exercising every class and every void."""
    rows, cols = np.mgrid[0:SIZE, 0:SIZE].astype(np.float64)
    metres = np.full((SIZE, SIZE), 2000.0)

    # A cone in the north-west quadrant whose flanks steepen with distance from
    # the summit — a quartic profile, so the angle runs from 0 at the top to
    # 87 degrees 600 m out and every band is crossed on the way down. Its
    # flanks face every bearing, so every octant and its edges are crossed too.
    distance = np.minimum(np.hypot(rows - 128, cols - 128) * 5.0, 600.0)
    cone = 3500.0 - 2.2e-8 * distance**4
    metres = np.where((rows < 256) & (cols < 256), cone, metres)

    # The north-east quadrant: planes in four strips — falling east at 30
    # degrees, north at 37, south-west at 42, and a gentle 3 degrees west. Not on
    # band edges: quarter-metre heights put a plane built at 30 degrees a hair
    # either side of it.
    for index, (east, south) in enumerate(
        [
            (-math.tan(math.radians(32)), 0.0),
            (0.0, math.tan(math.radians(37))),
            (
                math.tan(math.radians(42)) / math.sqrt(2),
                -math.tan(math.radians(42)) / math.sqrt(2),
            ),
            (math.tan(math.radians(3)), 0.0),
        ]
    ):
        strip = (rows < 256) & (cols >= 256) & (rows // 64 == index)
        metres = np.where(
            strip, 2500.0 + (cols - 256) * 5.0 * east + rows * 5.0 * south, metres
        )

    # The south-west quadrant: a flat lake, then rolling ground with a hole.
    lake = (rows >= 256) & (cols < 128)
    metres = np.where(lake, 372.0, metres)
    rolling = (rows >= 256) & (cols >= 128) & (cols < 256)
    metres = np.where(
        rolling, 1500.0 + 40.0 * np.sin(rows / 9.0) * np.cos(cols / 7.0), metres
    )

    stored = np.round(metres / 0.25).astype(np.int16)
    # A hole inside real ground: no data, and it voids every kernel touching it.
    stored[300:306, 180:184] = NODATA
    # A coverage edge: the south-east quadrant has no source at all.
    stored[256:, 256:] = NODATA
    return stored


@pytest.fixture(scope="module")
def grid() -> NDArray[np.int16]:
    return synthetic_grid()


def scalar_reference(grid: NDArray[np.int16]) -> NDArray[np.uint8]:
    """Classify every cell with the scalar kernel, one cell at a time."""
    rows, cols = grid.shape
    out = np.empty((rows, cols, 4), dtype=np.uint8)
    values = grid.tolist()
    for row in range(rows):
        for col in range(cols):
            kernel = []
            for row_offset, col_offset in KERNEL_OFFSETS:
                r, c = row + row_offset, col + col_offset
                # Off the raster is a hole, as an absent tile is to sample_slope.
                inside = 0 <= r < rows and 0 <= c < cols
                kernel.append(values[r][c] if inside else NODATA)
            out[row, col] = classify_kernel(kernel)
    return out


def vectorised(grid: NDArray[np.int16], strip_rows: int = 64) -> NDArray[np.uint8]:
    """Run the streaming classifier and read its three planes back."""
    sinks = (io.BytesIO(), io.BytesIO(), io.BytesIO())
    classify_grid(grid, sinks, strip_rows)
    planes = [
        np.frombuffer(sink.getvalue(), dtype=np.uint8).reshape(grid.shape)
        for sink in sinks
    ]
    alpha = np.full(grid.shape, 255, dtype=np.uint8)
    return np.stack([*planes, alpha], axis=-1)


@pytest.fixture(scope="module")
def reference(grid: NDArray[np.int16]) -> NDArray[np.uint8]:
    return scalar_reference(grid)


# --- Equal to the scalar kernel, everywhere ---------------------------------


def test_vectorised_equals_scalar_at_every_cell(
    grid: NDArray[np.int16], reference: NDArray[np.uint8]
) -> None:
    got = vectorised(grid)

    mismatched = np.argwhere((got != reference).any(axis=-1))
    assert not len(mismatched), f"{len(mismatched)} cells differ, first at " + str(
        mismatched[:5].tolist()
    )


@pytest.mark.parametrize("strip_rows", [1, 7, 512, 1000])
def test_strips_join_without_a_seam(
    grid: NDArray[np.int16], reference: NDArray[np.uint8], strip_rows: int
) -> None:
    # The halo is what makes strips independent of where they are cut. One row
    # per strip is the case where every kernel reaches into two other strips.
    assert np.array_equal(vectorised(grid, strip_rows), reference)


# --- The synthetic grid exercises what it claims to --------------------------


def test_every_octant_and_band_is_present(reference: NDArray[np.uint8]) -> None:
    classes = set(np.unique(reference[..., 2]).tolist())
    octants = {b >> 5 for b in classes if b < LEVEL}
    bands = {b & 31 for b in classes if b < LEVEL}

    assert octants == set(range(len(OCTANTS)))
    assert bands == set(range(18))
    assert LEVEL in classes
    assert NO_DATA in classes


@pytest.mark.parametrize(
    ("row", "col", "band", "octant"),
    [
        (32, 400, 6, "E"),  # 32 degrees, falling east
        (96, 400, 7, "N"),  # 37 degrees, falling north
        (160, 400, 8, "SW"),  # 42 degrees, falling south-west
        (224, 400, 0, "W"),  # 3 degrees, falling west
    ],
)
def test_the_planes_classify_as_built(
    reference: NDArray[np.uint8], row: int, col: int, band: int, octant: str
) -> None:
    decoded = decode_pixel(*reference[row, col].tolist())

    assert (decoded.band, decoded.octant) == (band, octant)


def test_the_lake_is_level_and_has_a_height(reference: NDArray[np.uint8]) -> None:
    decoded = decode_pixel(*reference[400, 60].tolist())

    assert decoded.is_level
    assert decoded.height_m == 372


def test_the_hole_voids_every_kernel_that_touches_it(
    reference: NDArray[np.uint8],
) -> None:
    step = WINDOW_STEP_CELLS
    # Cells whose kernel reaches the hole (rows 300-305, cols 180-183), even
    # though their own cell has a height.
    for row, col in [(298, 180), (300, 178), (307, 185), (303, 185)]:
        assert tuple(reference[row, col]) == NODATA_PIXEL
    # The kernel is nine cells two apart, not a 5x5 block: a cell whose nine
    # samples all miss the hole is classified even beside it, exactly as
    # sample_slope would classify it.
    assert reference[303, 186, 2] != NO_DATA
    assert reference[300 - 2 * step, 180, 2] != NO_DATA


def test_the_coverage_edge_is_nodata_not_a_cliff(
    reference: NDArray[np.uint8],
) -> None:
    # The last row of real ground before the south-east quadrant's nodata: its
    # kernel reaches into nothing, so it is unknown rather than a 2000 m drop.
    assert tuple(reference[254, 300]) == NODATA_PIXEL
    assert tuple(reference[300, 254]) == NODATA_PIXEL
    assert reference[253, 300, 2] != NO_DATA


def test_the_raster_edge_is_nodata(reference: NDArray[np.uint8]) -> None:
    assert (reference[0:2, :, 2] == NO_DATA).all()
    assert (reference[:, 0:2, 2] == NO_DATA).all()
    assert reference[2, 2, 2] != NO_DATA


# --- Edges of the classes ----------------------------------------------------


def test_a_cell_on_a_band_edge_takes_the_scalar_answer() -> None:
    # A 45-degree plane is exactly on the 45-50 edge: atan(1) in degrees is
    # 45.0 to the last bit in CPython, and numpy must not land on 44.99...
    block = np.zeros((5, 5), dtype=np.int16)
    for col in range(5):
        block[:, col] = 4000 + col * 20  # 5 m per 5 m cell east
    _, _, b = classify_block(block)

    assert (
        b[0, 2]
        == classify_kernel([int(block[2 + r, 2 + c]) for r, c in KERNEL_OFFSETS])[2]
    )
    assert b[0, 2] == OCTANTS.index("W") << 5 | 9


def test_a_block_with_no_interior_is_refused() -> None:
    with pytest.raises(ValueError, match="halo"):
        classify_block(np.zeros((4, 10), dtype=np.int16))


def test_a_narrow_block_is_all_nodata() -> None:
    _, _, b = classify_block(np.zeros((5, 4), dtype=np.int16))

    assert (b == NO_DATA).all()


def test_a_height_below_sea_level_is_refused() -> None:
    with pytest.raises(ValueError, match="below 0 m"):
        classify_block(np.full((5, 5), -40, dtype=np.int16))


# --- Files -------------------------------------------------------------------


def test_main_writes_planes_and_a_vrt(
    tmp_path: Path, grid: NDArray[np.int16], reference: NDArray[np.uint8]
) -> None:
    raster = tmp_path / "grid.raw"
    raster.write_bytes(grid.astype("<i2").tobytes())
    out = tmp_path / "classes"

    args = ["--raster", str(raster), "--west", "0", "--north", str(2 * TILE_SIZE_M)]
    args += ["--cols", str(SIZE), "--rows", str(SIZE), "--out", str(out)]
    assert main(args) == 0

    for index, plane in enumerate("rgb"):
        written = np.fromfile(out / f"class_{plane}.raw", dtype=np.uint8)
        assert np.array_equal(written.reshape(grid.shape), reference[..., index])
    assert not list(out.glob("*.part"))
    assert "<SRS>EPSG:3035</SRS>" in (out / "classes.vrt").read_text()


def test_main_refuses_an_unaligned_corner(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["--raster", "x", "--west", "5", "--north", "0", "--cols", "1"]
            + ["--rows", "1", "--out", str(tmp_path)]
        )


def test_main_fails_on_an_empty_grid(tmp_path: Path) -> None:
    raster = tmp_path / "grid.raw"
    raster.write_bytes(np.full((8, 8), NODATA, dtype="<i2").tobytes())

    args = ["--raster", str(raster), "--west", "0", "--north", "0"]
    assert main([*args, "--cols", "8", "--rows", "8", "--out", str(tmp_path)]) == 1


def test_read_grid_checks_the_size(tmp_path: Path) -> None:
    raster = tmp_path / "grid.raw"
    raster.write_bytes(b"\0" * 10)

    with pytest.raises(ValueError, match="10 bytes"):
        read_grid(raster, 4, 4)


def test_vrt_georeferences_the_planes() -> None:
    doc = vrt(4004480, 2754560, 512, 256)

    assert "<GeoTransform>4004480, 5, 0, 2754560, 0, -5</GeoTransform>" in doc
    assert doc.count("VRTRawRasterBand") == 3
    assert "<LineOffset>512</LineOffset>" in doc
    # No nodata: 255 is a class byte, and a declared nodata would make the warp
    # treat every unknown pixel as a hole to fill from its neighbours.
    assert "NoDataValue" not in doc


@pytest.mark.skipif(shutil.which("gdalinfo") is None, reason="GDAL not installed")
def test_gdal_reads_the_vrt(tmp_path: Path, grid: NDArray[np.int16]) -> None:
    raster = tmp_path / "grid.raw"
    raster.write_bytes(grid.astype("<i2").tobytes())
    args = ["--raster", str(raster), "--west", "0", "--north", str(2 * TILE_SIZE_M)]
    assert main([*args, "--cols", "512", "--rows", "512", "--out", str(tmp_path)]) == 0

    info = subprocess.run(  # noqa: S603
        ["gdalinfo", str(tmp_path / "classes.vrt")],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Size is 512, 512" in info
    assert "Band 3" in info
