"""Tests for cutting the warped raster into terrain tiles (SNOW-908).

The cutter is pure addressing: which bytes of a flat raster become which tile,
and where the skirt comes from. None of that fails visibly — a tile cut one row
out of place still decodes to plausible heights everywhere — so the tests read a
synthetic raster back through the *published* addressing (``cell_for``,
``read_cell``) rather than through the cutter's own arithmetic. Agreeing with
itself is not the property that matters.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cut_terrain_tiles import Raster, cut, grid_json  # noqa: E402
from terrain_grid import (  # noqa: E402
    CELL_SIZE_M,
    NODATA,
    SKIRT_CELLS,
    STORED_CELLS,
    TILE_BYTES,
    TILE_CELLS,
    TILE_SIZE_M,
    cell_for,
    decode_height,
    encode_height,
    read_cell,
)

# Three tile columns by two tile rows, with the easternmost column left empty.
WEST, NORTH = 0, 2 * TILE_SIZE_M
COLS, ROWS = 3 * TILE_CELLS, 2 * TILE_CELLS
EMPTY_FROM_COL = 2 * TILE_CELLS


def height_at(row: int, col: int) -> float:
    """Return a plane whose every step lands exactly on the stored scale."""
    return 1000.0 + row * 0.25 + col * 0.5


def build_raster(tmp_path: Path) -> Path:
    """Write a synthetic flat Int16 raster covering the extent above."""
    path = tmp_path / "grid.raw"
    with path.open("wb") as handle:
        for row in range(ROWS):
            values = [
                NODATA if col >= EMPTY_FROM_COL else encode_height(height_at(row, col))
                for col in range(COLS)
            ]
            handle.write(struct.pack(f"<{COLS}h", *values))
    return path


@pytest.fixture
def cut_tiles(tmp_path: Path) -> Path:
    out = tmp_path / "terrain"
    raster = Raster(build_raster(tmp_path), WEST, NORTH, COLS, ROWS)
    try:
        cut(raster, out)
    finally:
        raster.close()
    return out


def tile_body(out: Path, tile_x: int, tile_y: int) -> bytes:
    return (out / str(tile_x) / f"{tile_y}.s16").read_bytes()


# --- What gets written ------------------------------------------------------


def test_every_tile_is_exactly_one_tile_long(cut_tiles: Path) -> None:
    bodies = sorted(cut_tiles.rglob("*.s16"))

    assert bodies
    assert all(path.stat().st_size == TILE_BYTES for path in bodies)


def test_tiles_with_no_data_are_not_written(cut_tiles: Path) -> None:
    # Switzerland is a diagonal country in a rectangular grid, so about half the
    # tiles in the extent hold no ground. Writing them would double the object
    # count to say nothing; the Worker answers 204 instead.
    assert (cut_tiles / "0" / "0.s16").exists()
    assert (cut_tiles / "1" / "1.s16").exists()
    assert not (cut_tiles / "2").exists()


def test_coverage_reports_what_was_actually_written(tmp_path: Path) -> None:
    out = tmp_path / "terrain"
    raster = Raster(build_raster(tmp_path), WEST, NORTH, COLS, ROWS)
    try:
        coverage = cut(raster, out)
    finally:
        raster.close()

    assert coverage["tile_count"] == 4
    assert coverage["tile_x"] == [0, 1]
    assert coverage["tile_y"] == [0, 1]
    # The bbox is the populated area, not the extent that was warped.
    assert coverage["bbox"] == [0, 0, 2 * TILE_SIZE_M, 2 * TILE_SIZE_M]


# --- Whether the bytes are where the contract says they are -----------------


@pytest.mark.parametrize(
    ("easting", "northing"),
    [
        (2.5, 2557.5),  # north-west corner cell of the extent
        (7.5, 2552.5),
        (1277.5, 1282.5),  # last data cell of tile (0, 1)
        (1282.5, 1277.5),  # first data cell of tile (1, 0)
        (2557.5, 2.5),  # south-east of the populated area
        (640.0, 1920.0),  # somewhere in the middle
    ],
)
def test_a_coordinate_reads_the_height_the_raster_holds_there(
    cut_tiles: Path, easting: float, northing: float
) -> None:
    # The end-to-end property: project a coordinate to a tile and a cell with
    # the published arithmetic, fetch that tile, decode that cell, and get the
    # height the raster had at that coordinate. A row or column offset anywhere
    # in the cutter breaks this and nothing else notices.
    # Indexed with the grid's own boundary rule, not GDAL's — see
    # test_a_northing_on_a_cell_boundary_belongs_to_the_cell_above.
    raster_row = math.ceil((NORTH - northing) / CELL_SIZE_M) - 1
    raster_col = math.floor((easting - WEST) / CELL_SIZE_M)
    expected = height_at(raster_row, raster_col)

    cell = cell_for(easting, northing)
    body = tile_body(cut_tiles, cell.tile_x, cell.tile_y)

    assert read_cell(body, cell.row, cell.col) == expected


def test_the_skirt_holds_the_neighbours_edge(cut_tiles: Path) -> None:
    # A sample in the outermost data cell has to be able to read across the
    # boundary without fetching another tile. That is the entire justification
    # for storing 258 cells instead of 256.
    here = tile_body(cut_tiles, 0, 0)
    east = tile_body(cut_tiles, 1, 0)
    north = tile_body(cut_tiles, 0, 1)

    for row in (SKIRT_CELLS, 100, SKIRT_CELLS + TILE_CELLS - 1):
        assert read_cell(here, row, STORED_CELLS - 1) == read_cell(
            east, row, SKIRT_CELLS
        )

    for col in (SKIRT_CELLS, 100, SKIRT_CELLS + TILE_CELLS - 1):
        assert read_cell(here, 0, col) == read_cell(
            north, SKIRT_CELLS + TILE_CELLS - 1, col
        )


def test_a_skirt_over_nothing_is_nodata_not_an_error(cut_tiles: Path) -> None:
    # Tiles at the edge of the warped raster have skirts hanging off it, and
    # tiles at the edge of coverage have skirts over ground no source holds.
    # Both are absent heights, not failures — and absent is distinguishable.
    west_edge = tile_body(cut_tiles, 0, 0)
    coverage_edge = tile_body(cut_tiles, 1, 0)

    assert read_cell(west_edge, 100, 0) is None
    assert read_cell(coverage_edge, 100, STORED_CELLS - 1) is None
    assert read_cell(coverage_edge, 100, SKIRT_CELLS) is not None


def test_a_northing_on_a_cell_boundary_belongs_to_the_cell_above(
    cut_tiles: Path,
) -> None:
    # Cells are half-open the same way tiles are: [south, north). A coordinate
    # exactly on a horizontal cell boundary reads the cell to the *north* of it.
    #
    # This differs from GDAL's pixel rule, which would take the cell to the
    # south, and the difference is deliberate: tile_for puts a coordinate on a
    # tile's south edge inside that tile, so the southernmost row has to own its
    # south edge too or the two would disagree at every tile boundary. It only
    # bites on exact multiples of the cell size, but SNOW-917 has to apply the
    # same rule to read the same cell.
    boundary = cell_for(640.0, 1920.0)
    just_above = cell_for(640.0, 1920.0 + CELL_SIZE_M / 2)
    just_below = cell_for(640.0, 1920.0 - CELL_SIZE_M / 2)

    assert (boundary.row, boundary.col) == (just_above.row, just_above.col)
    assert boundary.row != just_below.row


def test_rows_are_written_north_first(cut_tiles: Path) -> None:
    # The one error that leaves every height plausible while putting the
    # Jungfrau on the Mittelland.
    body = tile_body(cut_tiles, 0, 1)
    northernmost = read_cell(body, SKIRT_CELLS, SKIRT_CELLS)
    southernmost = read_cell(body, SKIRT_CELLS + TILE_CELLS - 1, SKIRT_CELLS)

    assert northernmost == decode_height(encode_height(height_at(0, 0)))
    assert southernmost is not None
    assert northernmost is not None
    assert northernmost < southernmost  # the plane rises with raster row


# --- Refusing a raster that is not what it claims to be ---------------------


def test_a_raster_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    # The commonest way to get here is a wrong -te or -tr on the warp, and it
    # would otherwise produce a grid that is silently sheared.
    path = build_raster(tmp_path)
    path.write_bytes(path.read_bytes()[:-2])

    with pytest.raises(ValueError, match="bytes"):
        Raster(path, WEST, NORTH, COLS, ROWS)


def test_a_misaligned_raster_corner_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tile boundary"):
        Raster(build_raster(tmp_path), WEST + CELL_SIZE_M, NORTH, COLS, ROWS)


def test_a_raster_that_is_not_whole_tiles_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="whole number"):
        Raster(build_raster(tmp_path), WEST, NORTH, COLS + 1, ROWS)


def test_an_extent_with_no_data_at_all_is_an_error(tmp_path: Path) -> None:
    # Publishing nothing successfully is worse than failing: the upload would
    # succeed, verify.sh would 204 everywhere, and it would read as coverage
    # that has not been built yet rather than as a build that went wrong.
    path = tmp_path / "empty.raw"
    path.write_bytes(struct.pack("<h", NODATA) * COLS * ROWS)
    raster = Raster(path, WEST, NORTH, COLS, ROWS)
    try:
        with pytest.raises(RuntimeError, match="no tile"):
            cut(raster, tmp_path / "out")
    finally:
        raster.close()


# --- The published grid.json ------------------------------------------------


def test_grid_json_carries_the_source_and_its_coverage() -> None:
    doc = grid_json(
        {"tile_count": 4, "tile_x": [0, 1], "tile_y": [0, 1], "bbox": [0, 0, 1, 1]},
        "swissalti3d",
        "https://tiles.snowdesk-data.info",
        "v1",
        {"survey_years": [2019], "count": 42000},
    )
    source = doc["sources"][0]

    assert doc["crs"] == "EPSG:3035"
    assert source["id"] == "swissalti3d"
    # Native resolution travels with every height because cell spacing is not
    # information content: a 30 m source resampled onto this grid is honest only
    # while nothing downstream reads 5 m cells as 5 m of detail.
    assert source["native_resolution_m"] == 2.0
    assert source["attribution"] == "© swisstopo"
    assert source["coverage"]["tile_count"] == 4
    assert source["survey_years"] == [2019]
    assert json.dumps(doc)  # serialisable, since it is published as-is


def test_an_unregistered_source_is_refused() -> None:
    # Adding a source means adding its licence, its attribution and its native
    # resolution, not just pointing the build at different files.
    with pytest.raises(ValueError, match="unknown terrain source"):
        grid_json({"tile_count": 1}, "glo30", "", "", None)
