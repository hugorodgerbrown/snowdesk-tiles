"""Tests for the terrain grid definition (SNOW-908).

This module is a contract with another repository: SNOW-917 decodes these tiles
on the Django side using the numbers published in ``grid.json``. A change here
that nothing in this repo notices is a change that returns plausible, silently
wrong heights over there — so these tests are written to fail loudly on the
geometry and the encoding rather than to exercise the code.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

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

# The terrain route lives in the tileset table index.js imports (SNOW-987).
WORKER_SOURCE = ROOT / "worker" / "src" / "tilesets.js"

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
    assert r"/^\/terrain\/([^/]+)\/(\d+)\/(\d+)\.s16$/" in worker
    assert r"/^\/terrain\/([^/]+)\/grid\.json$/" in worker
    assert 'descriptorKey: "grid.json"' in worker


def test_definition_publishes_the_boundary_rule() -> None:
    # Not GDAL's rule, so leaving it implied would have SNOW-917 reading the
    # cell to the south of the one this grid says it is reading, on any
    # coordinate landing exactly on a boundary.
    rule = definition()["cell_boundary_rule"]

    assert "[south, north)" in rule
    assert "north and east" in rule


# --- Sources beyond Switzerland (SNOW-1505) ---------------------------------


def test_the_registry_holds_swissalti3d_and_one_mapterhorn_entry() -> None:
    from terrain_grid import SOURCES

    assert list(SOURCES) == ["swissalti3d", "mapterhorn"]
    mapterhorn = SOURCES["mapterhorn"]
    # Every field SNOW-917's source_from_payload requires, bar the per-build
    # resolution and coverage that grid_json adds.
    for key in (
        "id",
        "name",
        "provider",
        "quality",
        "attribution",
        "licence",
        "licence_url",
        "source_url",
    ):
        assert mapterhorn[key], key
    assert mapterhorn["quality"] == "lidar"
    assert mapterhorn["native_resolution_m"] is None


def test_the_attribution_credits_both_sources() -> None:
    from terrain_grid import ATTRIBUTION, SWISS_ONLY_ATTRIBUTION

    assert ATTRIBUTION == "© swisstopo, Mapterhorn"
    assert definition()["attribution"] == ATTRIBUTION
    assert SWISS_ONLY_ATTRIBUTION == "© swisstopo"


def test_the_class_tiles_carry_the_same_attribution() -> None:
    from terrain_class import descriptor

    assert descriptor()["attribution"] == "© swisstopo, Mapterhorn"


# --- Seam profiles ------------------------------------------------------------


def ramp(step_at_seam: float) -> tuple[list[float], list[float | None]]:
    """Return a 2 km, 5 m profile on a 10% ramp with a step at distance 0."""
    from terrain_grid import profile_points

    distances = [sample[0] for sample in profile_points(7.7, 46.0, 180)]
    heights: list[float | None] = [
        2000.0 + 0.1 * d + (step_at_seam if d > 0 else 0.0) for d in distances
    ]
    return distances, heights


def test_profile_points_are_2km_at_5m_centred_on_the_point() -> None:
    from terrain_grid import profile_points

    samples = profile_points(7.711, 45.944, 180)
    distances = [sample[0] for sample in samples]

    assert len(samples) == 401
    assert distances[0] == -1000.0
    assert distances[-1] == 1000.0
    assert samples[200][1:] == (7.711, 45.944)
    # Bearing 180 runs south: the far end is south of the centre.
    assert samples[-1][2] < 45.944 < samples[0][2]
    e0, n0 = lonlat_to_grid(*samples[0][1:])
    e1, n1 = lonlat_to_grid(*samples[1][1:])
    assert ((e1 - e0) ** 2 + (n1 - n0) ** 2) ** 0.5 == pytest.approx(5.0, abs=0.05)


def test_a_half_metre_step_passes_and_a_four_metre_step_fails() -> None:
    from terrain_grid import assess_profile, seam_step

    expect = {"seam_m": 2.5, "covered": [[5.0, 1000.0]], "foreign_covered": True}
    small = ramp(0.5)
    large = ramp(4.0)

    assert seam_step(*small, seam_m=2.5) == pytest.approx(0.5, abs=1e-6)
    failures, step = assess_profile(*small, expect, 3.0)
    assert failures == []
    assert step == pytest.approx(0.5)
    failures, step = assess_profile(*large, expect, 3.0)
    assert step == pytest.approx(4.0)
    assert failures == ["seam step 4.00 m exceeds 3 m"]


def test_a_continuing_slope_is_not_a_step() -> None:
    # Why a fit rather than the two nearest cells: on a 10% ramp, neighbours
    # 5 m apart differ by half a metre with no seam at all.
    from terrain_grid import seam_step

    assert seam_step(*ramp(0.0), seam_m=2.5) == pytest.approx(0.0, abs=1e-9)


def test_a_side_with_too_few_heights_has_no_step() -> None:
    from terrain_grid import seam_step

    distances, heights = ramp(0.0)
    heights = [h if d < 0 else None for d, h in zip(distances, heights, strict=True)]

    with pytest.raises(ValueError, match="far side"):
        seam_step(distances, heights, seam_m=2.5)


def test_a_covered_cell_without_a_height_fails() -> None:
    from terrain_grid import assess_profile

    distances, heights = ramp(0.0)
    heights[300] = None  # 500 m into the covered foreign side
    expect = {"seam_m": 2.5, "covered": [[5.0, 1000.0]], "foreign_covered": True}

    failures, _ = assess_profile(distances, heights, expect, 3.0)

    assert failures == ["no height inside coverage at 500 m"]


def test_an_uncovered_foreign_side_must_read_no_height() -> None:
    # Theodul, if the Aosta side has no national model: the Italian side is
    # 204, and a height there would be GLO-30 that slipped past the clip.
    from terrain_grid import assess_profile

    distances, heights = ramp(0.0)
    expect = {"seam_m": 2.5, "covered": [], "foreign_covered": False}
    absent = [h if d < 2.5 else None for d, h in zip(distances, heights, strict=True)]

    assert assess_profile(distances, absent, expect, 3.0) == ([], None)
    failures, _ = assess_profile(distances, heights, expect, 3.0)
    assert len(failures) == 199
    assert "no national model" in failures[0]


def test_sample_heights_reads_a_204_as_no_height() -> None:
    from terrain_grid import sample_heights

    fetched: list[str] = []

    def fetch(url: str) -> bytes | None:
        fetched.append(url)
        return None

    heights = sample_heights("https://t.example/terrain/v2/", [(7.7, 46.0)] * 3, fetch)

    assert heights == [None, None, None]
    assert len(fetched) == 1  # one tile, fetched once
    assert fetched[0].startswith("https://t.example/terrain/v2/")


# --- verify_points.json --------------------------------------------------------


def verify_points() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (ROOT / "scripts" / "verify_points.json").read_text()
    )
    return document


def test_every_summit_has_a_source_and_the_agreed_tolerance() -> None:
    summits = verify_points()["summits"]

    assert {s["name"].split(" (")[0] for s in summits} == {
        "Grossglockner",
        "Zugspitze",
        "Ortler",
        "Marmolada, Punta Penia",
        "Barre des Ecrins",
        "Aiguille du Midi",
    }
    for summit in summits:
        assert summit["height_source"]
        assert summit["height_url"].startswith("https://")
        assert summit["coordinates_source"].startswith("https://")
        # Published height -15 m to +2 m: a 5 m cell averages a summit down.
        assert summit["tolerance"] == {"below_m": 15, "above_m": 2}
        assert 5.0 <= summit["lon"] <= 16.6
        assert 43.7 <= summit["lat"] <= 48.4


def test_the_published_heights_are_the_agencies() -> None:
    heights = {s["name"]: s["height_m"] for s in verify_points()["summits"]}

    assert heights["Grossglockner (AT)"] == 3798
    assert heights["Zugspitze (DE)"] == 2962
    assert heights["Ortler (IT)"] == 3905
    assert heights["Marmolada, Punta Penia (IT)"] == 3343
    assert heights["Barre des Ecrins (FR)"] == 4102
    assert heights["Aiguille du Midi (FR)"] == 3842


def test_the_seam_profiles_are_theodul_and_piz_buin() -> None:
    profiles = {p["name"]: p for p in verify_points()["profiles"]}

    theodul = profiles["Theodul Pass (CH/IT)"]
    buin = profiles["Piz Buin (CH/AT)"]
    assert (theodul["lon"], theodul["lat"]) == (7.711, 45.944)
    assert (buin["lon"], buin["lat"]) == (10.118, 46.844)
    for profile in profiles.values():
        assert profile["length_m"] == 10000
        assert profile["step_m"] == 5
        assert profile["max_step_m"] == 3.0


def test_profile_points_cli_lists_every_sample(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from terrain_grid import main

    assert main(["profile-points"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 * 2001
    name, rest = lines[0].split("\t")
    assert name == "Theodul Pass (CH/IT)"
    assert len(rest.split()) == 5


# --- Checking a local tile tree before the upload ------------------------------


def write_tile_at(root: Path, lon: float, lat: float, metres: float) -> None:
    """Write a tile holding ``metres`` in every cell, under (lon, lat)."""
    cell = cell_for(*lonlat_to_grid(lon, lat))
    folder = root / str(cell.tile_x)
    folder.mkdir(parents=True, exist_ok=True)
    value = encode_height(metres)
    (folder / f"{cell.tile_y}.s16").write_bytes(
        struct.pack(f"<{STORED_CELLS * STORED_CELLS}h", *[value] * STORED_CELLS**2)
    )


def test_a_local_tree_reads_like_the_origin(tmp_path: Path) -> None:
    from terrain_grid import read_local_tile, sample_heights, tile_reader

    write_tile_at(tmp_path, 12.69444, 47.07472, 3790.0)

    heights = sample_heights(
        str(tmp_path),
        [(12.69444, 47.07472), (4.835, 45.764)],
        tile_reader(str(tmp_path)),
    )

    assert heights == [3790.0, None]  # Lyon: no file, read as a 204
    assert read_local_tile(str(tmp_path / "no" / "such.s16")) is None


def test_the_reader_is_http_for_a_url_and_refuses_anything_else(
    tmp_path: Path,
) -> None:
    from terrain_grid import fetch_tile, read_local_tile, tile_reader

    assert tile_reader("https://t.example/terrain/v2") is fetch_tile
    assert tile_reader(str(tmp_path)) is read_local_tile
    with pytest.raises(ValueError, match="neither an http"):
        tile_reader(str(tmp_path / "missing"))


def test_a_summit_outside_its_band_or_without_a_height_fails() -> None:
    from terrain_grid import check_summits

    band = {"below_m": 15, "above_m": 2}
    summits = [
        {"name": "Low", "height_m": 3000, "tolerance": band},
        {"name": "High", "height_m": 3000, "tolerance": band},
        {"name": "Gone", "height_m": 3000, "tolerance": band},
        {"name": "Fine", "height_m": 3000, "tolerance": band},
    ]

    failures = check_summits(summits, [2984.0, 3002.5, None, 2990.0])

    assert failures == [
        "Low: 2984.00 m, expected 2985-3002 m",
        "High: 3002.50 m, expected 2985-3002 m",
        "Gone: no height, expected 2985-3002 m",
    ]


def places_tree(root: Path) -> None:
    for summit in verify_points()["summits"]:
        write_tile_at(root, summit["lon"], summit["lat"], summit["height_m"] - 5)


def test_places_cli_passes_on_a_good_local_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from terrain_grid import main

    places_tree(tmp_path)
    expect = tmp_path / "verify-gap.json"
    expect.write_text(json.dumps({"gap": {"name": "Gap", "lon": 3.0, "lat": 44.0}}))

    assert main(["places", "--url", str(tmp_path), "--expect", str(expect)]) == 0

    out = capsys.readouterr().out
    assert "Zugspitze (DE): 2957.00 m (published 2962 m)" in out
    assert "Lyon: no tile, as expected" in out
    assert "Gap: no tile, as expected" in out


def test_places_cli_fails_on_a_stray_tile_and_warns_on_a_summit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from terrain_grid import main

    places_tree(tmp_path)
    zugspitze = next(s for s in verify_points()["summits"] if "Zugspitze" in s["name"])
    write_tile_at(tmp_path, zugspitze["lon"], zugspitze["lat"], 2900.0)

    assert main(["places", "--url", str(tmp_path)]) == 0
    assert "WARN Zugspitze (DE): 2900.00 m, expected 2947-2964 m" in (
        capsys.readouterr().out
    )

    write_tile_at(tmp_path, 4.835, 45.764, 200.0)  # Lyon has a tile

    assert main(["places", "--url", str(tmp_path)]) == 1
    assert "FAIL Lyon: a tile exists where none should" in capsys.readouterr().out
