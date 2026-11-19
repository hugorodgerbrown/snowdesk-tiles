"""Tests for the datum stop before the terrain build (SNOW-1505).

Synthetic ground on the grid's 5 m cells: a Swiss half and a foreign half of a
window, flat or steep, offset by a chosen amount. The threshold is the stop
condition, so it is tested either side of the line, and the slope filter is
tested by putting a wrong answer on steep ground where it must not count.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from datum_check import (  # noqa: E402
    MAX_DIFFERENCE_M,
    MIN_FLAT_CELLS,
    SEARCH_HALF_M,
    DatumResult,
    assess,
    assess_nearest_seam,
    clip_extent,
    crossing_box,
    load_points,
    main,
    slope_deg,
    strips,
    window,
)
from terrain_grid import lonlat_to_grid  # noqa: E402
from terrarium import ZOOM, tile_lonlat_bounds, tiles_for_box  # noqa: E402

SIZE = 60  # the 300 m window at 5 m


def halves(offset_m: float, tilt: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Return Swiss (west half) and foreign (east half) heights, NaN elsewhere.

    ``tilt`` is a rise per metre northwards; 0.005 is about 0.3 degree.
    """
    rows = np.arange(SIZE, dtype=np.float64)[:, None] * 5.0
    ground = 412.0 + tilt * (SIZE * 5.0 - rows) + np.zeros((SIZE, SIZE))
    swiss = ground.copy()
    swiss[:, SIZE // 2 :] = np.nan
    foreign = ground + offset_m
    foreign[:, : SIZE // 2 - 3] = np.nan  # the national model overlaps a little
    return swiss, foreign


def run(swiss: np.ndarray, foreign: np.ndarray) -> DatumResult:
    swiss_strip, foreign_strip = strips(swiss, foreign)
    return assess(swiss, swiss_strip, foreign, foreign_strip)


def test_six_decimetres_passes() -> None:
    result = run(*halves(0.6, tilt=0.005))

    assert result.ok, result.reason
    assert result.difference_m == pytest.approx(-0.6)


def test_one_and_a_half_metres_passes() -> None:
    # Stabio's measured step on 19 Nov 2026: model disagreement, not a datum.
    result = run(*halves(1.55, tilt=0.005))

    assert result.ok, result.reason


def test_three_and_a_half_metres_fails() -> None:
    result = run(*halves(3.5, tilt=0.005))

    assert not result.ok
    assert "more than 3 m" in result.reason


def test_the_threshold_is_three_metres() -> None:
    assert MAX_DIFFERENCE_M == 3.0
    assert MIN_FLAT_CELLS == 20


def test_steep_ground_is_too_few_flat_cells_and_fails() -> None:
    # A rise of 1 m per metre is 45 degrees: nothing here is flat.
    result = run(*halves(0.0, tilt=1.0))

    assert not result.ok
    assert "too few flat cells" in result.reason


def test_nineteen_flat_cells_are_not_enough() -> None:
    swiss = np.full((10, 10), 400.0)
    foreign = np.full((10, 10), 400.0)
    mask = np.zeros((10, 10), dtype=bool)
    mask.flat[:19] = True

    result = assess(swiss, mask, foreign, np.ones((10, 10), dtype=bool))

    assert not result.ok
    assert result.swiss_cells == 19


def test_only_cells_under_one_degree_count() -> None:
    # The foreign strip is half flat ground at +0.6 m and half a steep bank
    # fifty metres higher. Counted, the bank would fail the check; it is not
    # counted, because its slope is far over a degree.
    swiss = np.full((40, 40), 400.0)
    foreign = np.full((40, 40), 400.6)
    bank = np.arange(20, dtype=np.float64) * 5.0  # 45 degrees
    foreign[:, 20:] = 450.0 + bank[None, :]
    everything = np.ones((40, 40), dtype=bool)

    result = assess(swiss, everything, foreign, everything)

    assert result.ok, result.reason
    assert result.foreign_mean_m == pytest.approx(400.6)
    assert result.foreign_cells < 40 * 40


def test_a_foreign_side_with_no_model_says_so() -> None:
    swiss, foreign = halves(0.0)
    foreign[:] = np.nan

    result = run(swiss, foreign)

    assert not result.ok
    assert "outside every national model" in result.reason


def test_strips_hug_the_border_where_swiss_data_stops() -> None:
    swiss, foreign = halves(0.0)

    swiss_strip, foreign_strip = strips(swiss, foreign)

    swiss_cols = np.nonzero(swiss_strip.any(axis=0))[0]
    foreign_cols = np.nonzero(foreign_strip.any(axis=0))[0]
    # 25 m is five cells each side of column 30, where swissALTI3D stops.
    assert list(swiss_cols) == [25, 26, 27, 28, 29]
    assert list(foreign_cols) == [30, 31, 32, 33, 34]
    # A 200 m stretch: no further than 100 m from the centre along the border.
    rows = np.nonzero(swiss_strip.any(axis=1))[0]
    assert (rows.max() - rows.min()) * 5.0 <= 200.0


def test_slope_of_a_flat_plane_is_zero() -> None:
    assert np.allclose(slope_deg(np.full((5, 5), 10.0), 5.0), 0.0)


def test_the_window_is_cell_aligned_and_300m() -> None:
    west, south, east, north = window(9.655, 47.386)

    assert west % 5 == 0 and south % 5 == 0
    assert (east - west, north - south) == (300.0, 300.0)


def test_datum_points_hold_the_four_crossings() -> None:
    crossings = {c["name"]: (c["lon"], c["lat"]) for c in load_points()}

    assert crossings == {
        "Diepoldsau-Hohenems (CH/AT)": (9.655, 47.386),
        "Kreuzlingen-Konstanz (CH/DE)": (9.172, 47.656),
        "Meyrin-Saint-Genis-Pouilly (CH/FR)": (6.050, 46.243),
        "Stabio-Gaggiolo (CH/IT)": (8.915, 45.850),
    }


def test_every_crossing_window_fetches_the_tiles_it_touches() -> None:
    # Two crossings' windows hold no z14 tile centre, so a centre-in-area
    # listing would fetch nothing and stop every build at the datum gate.
    for crossing in load_points():
        box = crossing_box(crossing["lon"], crossing["lat"])
        tiles = tiles_for_box(box, ZOOM)
        assert tiles, crossing["name"]
        west = min(tile_lonlat_bounds(x, y, ZOOM)[0] for x, y in tiles)
        south = min(tile_lonlat_bounds(x, y, ZOOM)[1] for x, y in tiles)
        east = max(tile_lonlat_bounds(x, y, ZOOM)[2] for x, y in tiles)
        north = max(tile_lonlat_bounds(x, y, ZOOM)[3] for x, y in tiles)
        assert west <= box[0] and south <= box[1], crossing["name"]
        assert east >= box[2] and north >= box[3], crossing["name"]


def test_the_clip_extent_is_the_bbox_when_it_holds_every_crossing() -> None:
    alps = (5.0, 43.7, 16.6, 48.4)

    assert clip_extent(alps, load_points()) == alps


def test_the_clip_extent_widens_to_a_crossing_outside_the_bbox() -> None:
    # A northern bound of 47.6 leaves Kreuzlingen-Konstanz (47.656) outside.
    west, south, east, north = clip_extent((5.0, 43.7, 16.6, 47.6), load_points())

    assert (west, south, east) == (5.0, 43.7, 16.6)
    assert north == pytest.approx(47.656 + 0.027)


def test_print_extent_prints_the_box(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--print-extent", "5.0", "43.7", "16.6", "47.6"]) == 0

    assert capsys.readouterr().out.split() == [
        "5.000000",
        "43.700000",
        "16.600000",
        "47.683000",
    ]


SEARCH = 200  # a 1 km search grid at 5 m: enough for these synthetic seams


def search_grids(
    seam_col: int, offset_m: float = 0.0, steep_rows: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Return a search grid where swissALTI3D stops at ``seam_col``.

    The national model is everywhere, swissALTI3D west of the seam only, as
    where swissALTI3D carries on past the border. The first ``steep_rows``
    rows are a 45 degree slope.
    """
    ground = np.full((SEARCH, SEARCH), 412.0)
    cols = np.arange(SEARCH, dtype=np.float64)[None, :] * 5.0
    ground[:steep_rows, :] = 412.0 + np.broadcast_to(cols, (steep_rows, SEARCH))
    swiss = ground.copy()
    swiss[:, seam_col:] = np.nan
    return swiss, ground + offset_m


def test_the_seam_is_found_away_from_the_crossing() -> None:
    # The seam is 40 cells (200 m) east of the centre: outside a 300 m window
    # round the crossing, which is what stopped the VM run on 19 Nov 2026.
    swiss, foreign = search_grids(seam_col=SEARCH // 2 + 40, offset_m=0.6)

    result = assess_nearest_seam(swiss, foreign)

    assert result.ok, result.reason
    assert result.difference_m == pytest.approx(-0.6)
    assert "at the seam 19" in result.reason or "at the seam 20" in result.reason


def test_three_and_a_half_metres_at_the_seam_fails() -> None:
    swiss, foreign = search_grids(seam_col=SEARCH // 2 + 40, offset_m=3.5)

    result = assess_nearest_seam(swiss, foreign)

    assert not result.ok
    assert "more than 3 m" in result.reason


def test_a_steep_nearest_seam_moves_on_to_a_flat_one() -> None:
    # The seam runs north to south; its northern 140 rows (700 m) are steep,
    # so the stretch nearest the centre has no flat cells.
    swiss, foreign = search_grids(seam_col=SEARCH // 2, offset_m=0.6, steep_rows=140)

    result = assess_nearest_seam(swiss, foreign)

    assert result.ok, result.reason


def test_swiss_data_over_the_whole_search_box_says_so() -> None:
    ground = np.full((SEARCH, SEARCH), 412.0)

    result = assess_nearest_seam(ground, ground.copy())

    assert not result.ok
    assert "covers the whole search box" in result.reason


def test_a_seam_with_no_national_model_beyond_it_says_so() -> None:
    swiss, foreign = search_grids(seam_col=SEARCH // 2)
    foreign[:] = np.nan

    result = assess_nearest_seam(swiss, foreign)

    assert not result.ok
    assert "outside every national model" in result.reason


def test_every_crossing_box_holds_its_search_grid() -> None:
    # The box is fetched in lon/lat, the grid is cut in EPSG:3035: no point on
    # the box's edge may fall inside the grid, or the grid's corners go unfetched.
    for crossing in load_points():
        lon, lat = crossing["lon"], crossing["lat"]
        west, south, east, north = window(lon, lat, SEARCH_HALF_M)
        b_west, b_south, b_east, b_north = crossing_box(lon, lat)
        for t in np.linspace(0.0, 1.0, 101):
            for x, y in (
                (b_west + t * (b_east - b_west), b_south),
                (b_west + t * (b_east - b_west), b_north),
                (b_west, b_south + t * (b_north - b_south)),
                (b_east, b_south + t * (b_north - b_south)),
            ):
                e, n = lonlat_to_grid(x, y)
                assert not (west < e < east and south < n < north), crossing["name"]


def test_a_failed_run_says_what_it_means_and_what_to_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import datum_check

    outcomes = iter(
        [
            DatumResult(60, 60, 400.0, 400.5, True, "means differ by +0.50 m"),
            DatumResult(60, 60, 400.0, 404.0, False, "means differ by -4.00 m"),
            DatumResult(0, 0, None, None, False, "no seam"),
        ]
    )

    def fake_check(*args: object) -> DatumResult:
        outcome = next(outcomes, None)
        if outcome is None:
            raise OSError("https://example.invalid/x.tif: timed out")
        return outcome

    monkeypatch.setattr(datum_check, "check_crossing", fake_check)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gdalwarp")
    code = main(
        ["--work", str(tmp_path), "--dem-url", "https://x", "--clip", str(tmp_path)]
    )

    out = capsys.readouterr()
    assert code == 1
    assert out.out.count("FAIL") == 3
    assert "nothing was uploaded" in out.err
    assert "47 m off" in out.err  # the step
    assert "could not compare" in out.err  # no seam
    assert "downloads resume" in out.err  # the download error
