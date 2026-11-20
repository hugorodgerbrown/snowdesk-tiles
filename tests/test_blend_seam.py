"""Tests for blending swissALTI3D's edge into the Mapterhorn fill (SNOW-1505).

The shape that matters is Theodul's: swissALTI3D stops part way across a
glacier and the fill, surveyed on thicker ice, starts 21 m higher. After the
blend there is no step at the edge, the ramp is no steeper than the step spread
over BLEND_CELLS, and every cell further in is swissALTI3D's to the bit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from blend_seam import (  # noqa: E402
    BLEND_CELLS,
    blend_block,
    blend_window,
    main,
    window,
)
from terrain_grid import NODATA  # noqa: E402

STEP = 84  # 21 m in quarter-metre steps


def theodul(
    rows: int = 40, cols: int = 160, edge: int = 100
) -> tuple[NDArray[np.int16], NDArray[np.int16]]:
    """Swiss heights west of ``edge``, the fill everywhere, 21 m higher."""
    ground = np.full((rows, cols), 12000, dtype=np.int16)
    swiss = ground.copy()
    swiss[:, edge:] = NODATA
    fill = ground + STEP
    return swiss, fill.astype(np.int16)


def test_the_step_becomes_a_ramp_ending_on_the_fill() -> None:
    swiss, fill = theodul()

    out = blend_block(swiss, fill)

    row = out[20].astype(int)
    assert row[99] == round(12000 + STEP * (1 - 1 / BLEND_CELLS))  # one cell in
    assert abs(int(fill[20, 100]) - row[99]) <= 2  # no step at the edge any more
    assert np.all(np.abs(np.diff(row[40:100])) <= 2)  # 84 over 50 cells
    assert np.all(row[: 100 - BLEND_CELLS] == 12000)  # swissALTI3D's, exactly


def test_cells_beyond_the_ramp_and_without_fill_keep_the_swiss_height() -> None:
    swiss, fill = theodul()
    fill[:10, :] = NODATA  # no national model under the top rows

    out = blend_block(swiss, fill)

    assert np.array_equal(out[:10, :100], swiss[:10, :100])
    assert np.array_equal(out[:, :49], swiss[:, :49])


def test_an_edge_facing_no_fill_is_left_alone() -> None:
    swiss, fill = theodul()
    fill[:, 100:] = NODATA  # the edge of every model: nothing to blend into

    assert np.array_equal(blend_block(swiss, fill)[:, :100], swiss[:, :100])


def test_blocks_join_without_a_seam_and_a_rerun_changes_nothing() -> None:
    swiss, fill = theodul(rows=300, cols=400, edge=230)
    whole = np.where(swiss != NODATA, swiss, fill).astype(np.int16)
    one = whole.copy()
    many = whole.copy()

    changed = blend_window(one, swiss, fill, 0, 0, block=1000)
    blend_window(many, swiss, fill, 0, 0, block=64)

    assert changed == 300 * (BLEND_CELLS - 1)  # the 50th step is all Swiss
    assert np.array_equal(one, many)
    assert blend_window(one, swiss, fill, 0, 0, block=64) == 0


def test_the_window_lands_at_its_offset_in_the_grid() -> None:
    swiss, fill = theodul()
    raw = np.full((100, 300), 7, dtype=np.int16)
    raw[30:70, 50:210] = np.where(swiss != NODATA, swiss, fill)

    blend_window(raw, swiss, fill, xoff=50, yoff=30)

    assert raw[0, 0] == 7
    assert raw[50, 50 + 99] != 12000  # blended, inside the window
    assert raw[50, 50 + 10] == 12000


def test_the_window_is_the_swiss_bbox_plus_a_tile_clipped_to_the_grid() -> None:
    west, north = 3_840_000, 2_820_000
    cols, rows = 184_832, 103_680

    x, y, width, height = window([5.9, 45.8, 10.5, 47.8], west, north, cols, rows)

    assert 0 <= x < x + width <= cols
    assert 0 <= y < y + height <= rows
    assert 60_000 < width < 80_000  # ~350 km at 5 m
    assert 40_000 < height < 55_000  # ~225 km


def test_a_bbox_off_the_grid_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the grid"):
        window([5.9, 45.8, 10.5, 47.8], 0, 0, 10, 10)


def test_cli_blends_in_place_and_refuses_a_short_window(tmp_path: Path) -> None:
    swiss, fill = theodul()
    raw = np.where(swiss != NODATA, swiss, fill).astype("<i2")
    paths = {name: tmp_path / f"{name}.raw" for name in ("raw", "swiss", "fill")}
    raw.tofile(paths["raw"])
    swiss.astype("<i2").tofile(paths["swiss"])
    fill.astype("<i2").tofile(paths["fill"])
    args = [
        "blend",
        "--raw", str(paths["raw"]), "--cols", "160", "--rows", "40",
        "--swiss", str(paths["swiss"]), "--fill", str(paths["fill"]),
        "--xoff", "0", "--yoff", "0", "--width", "160", "--height", "40",
    ]  # fmt: skip

    assert main(args) == 0
    blended = np.fromfile(paths["raw"], dtype="<i2").reshape(40, 160)
    assert blended[20, 99] != 12000

    paths["fill"].write_bytes(paths["fill"].read_bytes()[:-2])
    assert main(args) == 1
