"""Tests for the terrain grid definition (SNOW-908).

This module is a contract with another repository: SNOW-917 decodes these tiles
on the Django side using the numbers published in ``grid.json``. A change here
that nothing in this repo notices is a change that returns plausible, silently
wrong heights over there — so these tests are written to fail loudly on the
geometry and the encoding rather than to exercise the code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from terrain_grid import (  # noqa: E402
    CELL_SIZE_M,
    DEFAULT_ANALYSIS_WINDOW_M,
    HEIGHT_MAX_M,
    HEIGHT_MIN_M,
    HEIGHT_NODATA_M,
    HEIGHT_SCALE_M,
    NODATA,
    SKIRT_CELLS,
    STORED_CELLS,
    TILE_BYTES,
    TILE_CELLS,
    TILE_SIZE_M,
    cell_centre,
    cell_for,
    decode_height,
    definition,
    encode_height,
    lonlat_extent,
    lonlat_to_grid,
    read_cell,
    snap_extent,
    stored_bounds,
    tile_bounds,
    tile_for,
)

WORKER_SOURCE = ROOT / "worker" / "src" / "index.js"

# Generated with PROJ (pyproj, EPSG:4326 -> EPSG:3035) and pinned here so the
# stdlib projection in terrain_grid.py cannot drift away from the real thing
# without a test saying so. See that module for why it is hand-rolled at all.
CONTROL_POINTS = [
    # lon, lat, easting, northing
    (10.000000, 52.000000, 4321000.0000, 3210000.0000),  # the projection origin
    (7.438632, 46.951082, 4125888.5535, 2651989.0501),  # Bern
    (7.980556, 46.547222, 4165978.8934, 2605874.5169),  # Jungfraujoch
    (7.658611, 45.976389, 4139355.2144, 2543245.4100),  # Matterhorn
    (9.908333, 46.381389, 4313940.3885, 2585357.0082),  # Piz Bernina
    (7.748056, 46.020833, 4146435.1149, 2547963.3224),  # Zermatt
]


# --- Tile geometry ----------------------------------------------------------


def test_tile_bounds_are_the_tile_the_coordinate_is_in() -> None:
    west, south, east, north = tile_bounds(3238, 1990)

    assert tile_for(west, south) == (3238, 1990)
    assert tile_for((west + east) / 2, (south + north) / 2) == (3238, 1990)
    assert east - west == TILE_SIZE_M
    assert north - south == TILE_SIZE_M


def test_tile_edges_are_half_open_so_no_coordinate_lands_in_two_tiles() -> None:
    # A coordinate on a shared edge has to belong to exactly one tile, and the
    # rule has to be the same one SNOW-917 applies. Half-open upwards, matching
    # the cell indexing.
    west, south, east, north = tile_bounds(100, 200)

    assert tile_for(west, south) == (100, 200)
    assert tile_for(east, north) == (101, 201)


def test_first_data_cell_is_inside_the_skirt() -> None:
    west, _, _, north = tile_bounds(0, 0)

    north_west = cell_for(west + CELL_SIZE_M / 2, north - CELL_SIZE_M / 2)
    south_east = cell_for(
        west + TILE_SIZE_M - CELL_SIZE_M / 2,
        north - TILE_SIZE_M + CELL_SIZE_M / 2,
    )

    assert (north_west.row, north_west.col) == (SKIRT_CELLS, SKIRT_CELLS)
    assert (south_east.row, south_east.col) == (
        SKIRT_CELLS + TILE_CELLS - 1,
        SKIRT_CELLS + TILE_CELLS - 1,
    )


def test_rows_run_north_to_south() -> None:
    # A north-south flip is the failure this grid is most exposed to: every
    # height stays plausible and only the terrain moves. Pin the direction.
    _, _, _, north = tile_bounds(10, 10)
    higher = cell_for(tile_bounds(10, 10)[0] + 5, north - CELL_SIZE_M / 2)
    lower = cell_for(tile_bounds(10, 10)[0] + 5, north - TILE_SIZE_M / 2)

    assert higher.row < lower.row


@pytest.mark.parametrize("row", [0, 1, 128, STORED_CELLS - 1])
@pytest.mark.parametrize("col", [0, 1, 128, STORED_CELLS - 1])
def test_cell_centre_round_trips_through_cell_for(row: int, col: int) -> None:
    easting, northing = cell_centre(3238, 1990, row, col)
    found = cell_for(easting, northing)

    # Skirt cells resolve to their *owning* tile, which is the neighbour — the
    # same ground, addressed from the other side.
    owner_row = row + (TILE_CELLS if row < SKIRT_CELLS else 0)
    owner_row -= TILE_CELLS if row >= SKIRT_CELLS + TILE_CELLS else 0
    owner_col = col + (TILE_CELLS if col < SKIRT_CELLS else 0)
    owner_col -= TILE_CELLS if col >= SKIRT_CELLS + TILE_CELLS else 0

    assert (found.row, found.col) == (owner_row, owner_col)


def test_the_skirt_is_the_neighbours_first_data_column() -> None:
    # The whole point of storing a skirt: a sample in the outermost data cell
    # can read its neighbour without fetching the next tile. If these two cells
    # were not the same ground, every tile boundary would be a seam of wrong
    # answers 1280 m apart.
    east_skirt = cell_centre(50, 60, 10, STORED_CELLS - 1)
    neighbour_first = cell_centre(51, 60, 10, SKIRT_CELLS)

    assert east_skirt == neighbour_first

    south_skirt = cell_centre(50, 60, STORED_CELLS - 1, 10)
    below_first = cell_centre(50, 59, SKIRT_CELLS, 10)

    assert south_skirt == below_first


def test_stored_bounds_are_one_cell_larger_on_every_side() -> None:
    data = tile_bounds(7, 8)
    stored = stored_bounds(7, 8)

    assert stored[0] == data[0] - CELL_SIZE_M
    assert stored[1] == data[1] - CELL_SIZE_M
    assert stored[2] == data[2] + CELL_SIZE_M
    assert stored[3] == data[3] + CELL_SIZE_M


# --- Height encoding --------------------------------------------------------


@pytest.mark.parametrize("metres", [0.0, 372.0, 1608.25, 3466.5, 4634.0, -400.0])
def test_heights_round_trip_within_half_a_step(metres: float) -> None:
    decoded = decode_height(encode_height(metres))

    assert decoded is not None
    assert abs(decoded - metres) <= HEIGHT_SCALE_M / 2


def test_the_quantisation_step_is_below_the_sources_own_accuracy() -> None:
    # Not a style preference. Storing whole metres puts +/-0.5 m of independent
    # noise on each cell, which differenced across the default analysis window
    # is an error larger than the gap between the 35 and 40 degree classes this
    # data exists to separate. swissALTI3D's own vertical accuracy is 0.3-0.5 m
    # from LiDAR, so quantising finer than that is free of meaning-loss and
    # quantising coarser throws away real signal.
    assert HEIGHT_SCALE_M <= 0.3

    import math

    # Worst-case slope error from quantisation alone, over the default window.
    worst = math.degrees(math.atan(HEIGHT_SCALE_M / DEFAULT_ANALYSIS_WINDOW_M))
    assert worst < 1.5

    metre_quantised = math.degrees(math.atan(1.0 / DEFAULT_ANALYSIS_WINDOW_M))
    assert metre_quantised > 5.0  # what Int16 metres would have cost


def test_nodata_is_not_a_height() -> None:
    # The single failure this design exists to prevent: absent terrain must
    # never be readable as flat ground.
    assert decode_height(NODATA) is None
    assert decode_height(0) == 0.0


def test_the_representable_range_covers_the_alps() -> None:
    assert encode_height(4808.7)  # Mont Blanc, the highest ground in scope
    assert HEIGHT_MIN_M < 0 < HEIGHT_MAX_M

    with pytest.raises(ValueError):
        encode_height(HEIGHT_MAX_M + 1)
    with pytest.raises(ValueError):
        encode_height(HEIGHT_MIN_M - 1)


def test_the_nodata_sentinel_quantises_exactly_onto_nodata() -> None:
    # build-terrain.sh hands HEIGHT_NODATA_M to `gdalwarp -dstnodata` and lets
    # the linear -scale carry it down onto the stored NODATA. If that mapping
    # were off by one the border of coverage would decode as -8191.75 m of
    # terrain rather than as absent.
    assert HEIGHT_NODATA_M / HEIGHT_SCALE_M == NODATA


def test_read_cell_finds_the_right_offset() -> None:
    import struct

    body = bytearray(struct.pack("<h", NODATA) * (STORED_CELLS * STORED_CELLS))
    struct.pack_into("<h", body, (7 * STORED_CELLS + 9) * 2, encode_height(1234.5))

    assert read_cell(bytes(body), 7, 9) == 1234.5
    assert read_cell(bytes(body), 9, 7) is None


def test_read_cell_refuses_a_body_that_is_not_a_tile() -> None:
    # A truncated body would otherwise decode from the first row as if nothing
    # were wrong.
    with pytest.raises(ValueError):
        read_cell(b"\x00" * (TILE_BYTES - 2), 0, 0)


# --- Extents ----------------------------------------------------------------


def test_snap_extent_aligns_to_whole_tiles_and_covers_the_input() -> None:
    extent = snap_extent(100, 100, 2000, 2000, margin_tiles=0)

    assert (extent.west, extent.south) == (0, 0)
    assert (extent.east, extent.north) == (2 * TILE_SIZE_M, 2 * TILE_SIZE_M)
    assert (extent.tiles_x, extent.tiles_y) == (2, 2)
    assert extent.west % TILE_SIZE_M == 0
    assert (extent.east - extent.west) // TILE_SIZE_M == extent.tiles_x


def test_snap_extent_margin_adds_a_ring_of_tiles() -> None:
    bare = snap_extent(100, 100, 2000, 2000, margin_tiles=0)
    ringed = snap_extent(100, 100, 2000, 2000, margin_tiles=1)

    assert ringed.tiles_x == bare.tiles_x + 2
    assert ringed.tiles_y == bare.tiles_y + 2
    assert ringed.west == bare.west - TILE_SIZE_M
    assert ringed.north == bare.north + TILE_SIZE_M


def test_snap_extent_does_not_add_a_tile_for_a_box_on_the_boundary() -> None:
    extent = snap_extent(0, 0, TILE_SIZE_M, TILE_SIZE_M, margin_tiles=0)

    assert (extent.tiles_x, extent.tiles_y) == (1, 1)


# --- Projection -------------------------------------------------------------


@pytest.mark.parametrize(("lon", "lat", "easting", "northing"), CONTROL_POINTS)
def test_projection_agrees_with_proj(
    lon: float, lat: float, easting: float, northing: float
) -> None:
    got_e, got_n = lonlat_to_grid(lon, lat)

    assert abs(got_e - easting) < 0.001
    assert abs(got_n - northing) < 0.001


def test_projected_extent_is_wider_than_the_projected_corners() -> None:
    # LAEA bends straight lines, so a lon/lat box projects to a shape with bowed
    # edges. Taking the four corners would clip real coverage off the sides;
    # this is why lonlat_extent densifies.
    box = (5.95, 45.72, 10.50, 47.83)
    densified = lonlat_extent(*box)
    corners = [
        lonlat_to_grid(lon, lat) for lon in (box[0], box[2]) for lat in (box[1], box[3])
    ]
    corner_box = (
        min(p[0] for p in corners),
        min(p[1] for p in corners),
        max(p[0] for p in corners),
        max(p[1] for p in corners),
    )

    assert densified[0] <= corner_box[0]
    assert densified[1] <= corner_box[1]
    assert densified[2] >= corner_box[2]
    assert densified[3] >= corner_box[3]
    assert densified != corner_box


# --- The published definition -----------------------------------------------


def test_definition_is_internally_consistent() -> None:
    doc = definition()

    assert doc["stored_cells"] == doc["tile_cells"] + 2 * doc["skirt_cells"]
    assert doc["tile_bytes"] == doc["stored_cells"] ** 2 * 2
    assert doc["tile_size_m"] == doc["tile_cells"] * doc["cell_size_m"]
    assert doc["dtype"] == "int16"
    assert doc["byte_order"] == "little"


def test_definition_states_what_an_absent_tile_means() -> None:
    # SNOW-839 and SNOW-910 both turn on absent never rendering as gentle
    # ground, and the consumer is in another repository. Saying it in the
    # published document is the only place it travels with the data.
    assert "204" in definition()["absent_tile"]


def test_definition_only_claims_a_url_when_it_can_build_one() -> None:
    assert "tile_url_template" not in definition()
    assert "tile_url_template" not in definition(origin="https://example.test")

    doc = definition("https://tiles.snowdesk-data.info/", "v1")

    assert doc["tile_url_template"] == (
        "https://tiles.snowdesk-data.info/terrain/v1/{x}/{y}.s16"
    )


def test_cli_definition_is_valid_json() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "terrain_grid.py"), "definition"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout)["crs"] == "EPSG:3035"


def test_the_worker_serves_the_paths_this_module_publishes() -> None:
    # The URL template and the Worker's route are two statements of the same
    # thing in two languages. verify.sh catches a mismatch against the live
    # origin; this catches it before anything is deployed.
    worker = WORKER_SOURCE.read_text(encoding="utf-8")
    template = definition("https://example.test", "v1")["tile_url_template"]

    assert template.endswith("/terrain/v1/{x}/{y}.s16")
    assert r"/^\/terrain\/[^/]+\/(\d+)\/(\d+)\.s16$/" in worker
    assert "terrain/grid.json" in worker


def test_definition_publishes_the_boundary_rule() -> None:
    # Not GDAL's rule, so leaving it implied would have SNOW-917 reading the
    # cell to the south of the one this grid says it is reading, on any
    # coordinate landing exactly on a boundary.
    rule = definition()["cell_boundary_rule"]

    assert "[south, north)" in rule
    assert "north and east" in rule
