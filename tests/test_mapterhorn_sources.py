"""Tests for choosing and crediting the Mapterhorn sources (SNOW-1505).

The fixtures under tests/fixtures/mapterhorn stand in for Mapterhorn's
attribution.json and per-source coverage, which cannot be fetched from here:
a 4 x 4 block of z12 cells as the supported area, a GLO-30 polygon over
everything, an Austrian model over the area's east side, a Trentino sliver
that only an edge crosses into the area, and a French model far away.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "mapterhorn"
sys.path.insert(0, str(ROOT / "scripts"))

from mapterhorn_sources import (  # noqa: E402
    attribution_entries,
    cells_met,
    check_dem_zoom,
    contains,
    find_gap,
    intervals,
    is_glo30,
    main,
    mapterhorn_area_bbox,
    merge_manifest,
    parse_resolution,
    probe,
    profile_expectations,
    seam_from_swiss,
    select,
    source_id,
)

ALPS = (5.0, 43.7, 16.6, 48.4)
SWISS_BBOX = [5.95, 45.82, 9.6, 47.81]  # west of the fixture area, not touching


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def rect(w: float, s: float, e: float, n: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
    }


def feature(source: str, geometry: dict[str, Any]) -> Any:
    return {"type": "Feature", "properties": {"source": source}, "geometry": geometry}


@pytest.fixture
def selected() -> tuple[dict[str, Any], dict[str, Any]]:
    return select(
        load("coverage.geojson"), load("attribution.json"), load("area.geojson"), ALPS
    )


# --- What is kept -------------------------------------------------------------


def test_glo30_is_never_kept(selected: tuple[dict[str, Any], dict[str, Any]]) -> None:
    # GLO-30 covers the whole fixture area, and is still left out: a 30 m
    # radar height labelled lidar is what the clip exists to prevent.
    clip, credits = selected

    assert "glo30" not in {f["properties"]["source"] for f in clip["features"]}
    assert "glo30" not in {c["id"] for c in credits["credits"]}


def test_glo30_is_recognised_under_a_prefix() -> None:
    assert is_glo30("glo30")
    assert is_glo30("debug-glo30")
    assert not is_glo30("at1")


def test_only_sources_meeting_the_area_are_kept(
    selected: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    clip, credits = selected

    # Trentino's sliver has no vertex in the area and holds no cell centre;
    # only its edge crosses a cell. France is nowhere near.
    assert [c["id"] for c in credits["credits"]] == ["at1", "ittrentino"]
    assert [f["properties"]["source"] for f in clip["features"]] == [
        "at1",
        "ittrentino",
    ]


def test_polygons_are_kept_as_published(
    selected: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    clip, _ = selected
    published = {
        f["properties"]["source"]: f["geometry"]
        for f in load("coverage.geojson")["features"]
    }

    for kept in clip["features"]:
        assert kept["geometry"] == published[kept["properties"]["source"]]


def test_credits_carry_each_models_terms(
    selected: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    _, credits = selected
    at1 = credits["credits"][0]

    assert at1 == {
        "id": "at1",
        "name": "ALS-DGM Hoehenraster",
        "producer": "BEV",
        "licence": "CC-BY-4.0",
        "url": "https://www.bev.gv.at",
        "native_resolution_m": 1.0,
        "attribution": "© BEV",
    }
    # The mosaic's resolution is its coarsest model's: Trentino's 5 m.
    assert credits["native_resolution_m"] == 5.0


def test_a_kept_source_without_attribution_stops_the_build() -> None:
    attribution = [a for a in load("attribution.json") if a["source"] != "at1"]

    with pytest.raises(ValueError, match="no entry for at1"):
        select(load("coverage.geojson"), attribution, load("area.geojson"), ALPS)


def test_nothing_but_glo30_stops_the_build() -> None:
    coverage = {
        "type": "FeatureCollection",
        "features": [feature("glo30", rect(0, 40, 20, 50))],
    }

    with pytest.raises(ValueError, match="no national model"):
        select(coverage, load("attribution.json"), load("area.geojson"), ALPS)


def test_a_feature_without_a_source_id_is_an_error() -> None:
    with pytest.raises(ValueError, match="source-field"):
        source_id({"properties": {"colour": "red"}})
    assert source_id({"properties": {"slug": "x"}}, ("slug",)) == "x"


# --- Geometry -------------------------------------------------------------------


def test_a_polygon_holding_the_whole_area_meets_every_cell() -> None:
    cells = {(2160 + dx, 1440 + dy) for dx in range(4) for dy in range(4)}

    assert cells_met(rect(9.0, 46.0, 11.0, 48.0), cells, 12) == cells


def test_a_polygon_beside_the_area_meets_nothing() -> None:
    cells = {(2160, 1440)}

    # Shares the cell's west edge exactly; touching is not meeting.
    assert cells_met(rect(9.7, 46.9, 9.84375, 47.1), cells, 12) == set()


def test_contains_is_even_odd_with_holes() -> None:
    donut = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
        ],
    }

    assert contains(donut, [(0.5, 0.5), (2, 2), (5, 5)]) == [True, False, False]


# --- Attribution ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "metres"), [(1, 1.0), (2.5, 2.5), ("5 m", 5.0), ("0.5m", 0.5)]
)
def test_resolutions_read_as_metres(value: Any, metres: float) -> None:
    assert parse_resolution(value) == metres


def test_an_unreadable_resolution_is_an_error() -> None:
    with pytest.raises(ValueError, match="resolution"):
        parse_resolution("fine")


def test_attribution_reads_a_list_a_wrapper_or_a_mapping() -> None:
    listed = attribution_entries([{"source": "a", "name": "A"}])
    wrapped = attribution_entries({"sources": [{"id": "a", "name": "A"}]})
    mapped = attribution_entries({"a": {"name": "A"}})

    assert listed["a"]["name"] == wrapped["a"]["name"] == mapped["a"]["name"] == "A"


# --- The manifest -----------------------------------------------------------------


def test_the_manifest_bbox_is_the_union_of_both_sources() -> None:
    swiss = {"bbox": SWISS_BBOX, "count": 43650, "survey_years": [2019, 2024]}
    area_bbox = mapterhorn_area_bbox(load("area.geojson"), ALPS)
    _, credits = select(
        load("coverage.geojson"), load("attribution.json"), load("area.geojson"), ALPS
    )

    merged = merge_manifest(
        swiss, area_bbox, credits, {"source_release": "2026-11-01"}, "2026-12"
    )

    assert merged["bbox"] == [5.95, 45.82, area_bbox[2], 47.81]
    assert merged["count"] == 43650  # what cut_terrain_tiles reads, unchanged
    assert merged["sources"]["swissalti3d"]["bbox"] == SWISS_BBOX
    mapterhorn = merged["sources"]["mapterhorn"]
    assert mapterhorn["release"] == "2026-11-01"
    assert mapterhorn["dem_version"] == "2026-12"
    assert mapterhorn["native_resolution_m"] == 5.0
    assert [c["id"] for c in mapterhorn["credits"]] == ["at1", "ittrentino"]


def test_the_area_bbox_is_clipped_to_the_box() -> None:
    whole = mapterhorn_area_bbox(load("area.geojson"), ALPS)
    clipped = mapterhorn_area_bbox(load("area.geojson"), (9.0, 46.0, 10.0, 48.0))

    assert whole == pytest.approx((9.84375, 46.80006, 10.1953125, 47.04018), abs=1e-5)
    assert clipped[2] == pytest.approx(10.0, abs=0.03)  # to the next z14 edge


def test_a_copy_shallower_than_z14_cannot_feed_the_build() -> None:
    assert check_dem_zoom({"max_zoom": 15}) is None
    assert check_dem_zoom({"max_zoom": 14}) is None
    reason = check_dem_zoom({"max_zoom": 13})
    assert reason is not None and "z14" in reason
    assert check_dem_zoom({}) is not None


def test_dem_zoom_cli_exits_non_zero(tmp_path: Path) -> None:
    manifest = tmp_path / "dem.json"
    manifest.write_text('{"max_zoom": 12}')

    assert main(["dem-zoom", str(manifest)]) == 1
    manifest.write_text('{"max_zoom": 15}')
    assert main(["dem-zoom", str(manifest)]) == 0


# --- verify-gap ---------------------------------------------------------------------


def test_the_gap_is_inside_the_area_and_outside_every_model() -> None:
    clip = {
        "type": "FeatureCollection",
        "features": [feature("at1", rect(10.0, 46.0, 11.0, 48.0))],
    }

    gap = find_gap(clip, load("area.geojson"), ALPS, SWISS_BBOX)

    assert gap is not None
    assert 9.84375 < gap["lon"] < 10.0
    assert 46.80006 < gap["lat"] < 47.04018
    assert contains(clip["features"][0]["geometry"], [(gap["lon"], gap["lat"])]) == [
        False
    ]


def test_no_gap_when_the_models_cover_everything() -> None:
    clip = {
        "type": "FeatureCollection",
        "features": [feature("at1", rect(9.0, 46.0, 11.0, 48.0))],
    }

    assert find_gap(clip, load("area.geojson"), ALPS, SWISS_BBOX) is None


def test_the_gap_keeps_clear_of_switzerland() -> None:
    clip = {"type": "FeatureCollection", "features": []}
    swiss_over_the_area = [9.0, 46.0, 10.5, 48.0]

    assert find_gap(clip, load("area.geojson"), ALPS, swiss_over_the_area) is None


def test_the_seam_is_where_swissalti3d_stops() -> None:
    distances = [-10.0, -5.0, 0.0, 5.0, 10.0]

    assert seam_from_swiss(distances, [True, True, False, False, False]) == -2.5
    with pytest.raises(ValueError, match="does not stop"):
        seam_from_swiss(distances, [True] * 5)


def test_intervals_are_runs_of_covered_samples() -> None:
    distances = [0.0, 5.0, 10.0, 15.0, 20.0]

    assert intervals(distances, [True, True, False, True, False]) == [
        [0.0, 5.0],
        [15.0, 15.0],
    ]


def test_profile_expectations_mark_the_covered_foreign_side() -> None:
    clip = {
        "type": "FeatureCollection",
        "features": [feature("at1", rect(10.0, 46.85, 10.5, 46.95))],
    }
    samples = [(float(d), 10.0 + d * 1e-5, 46.9) for d in range(-50, 51, 5)]
    swiss = [d < 0 for d, _, _ in samples]

    expect = profile_expectations(clip, load("area.geojson"), samples, swiss)

    assert expect["seam_m"] == -2.5
    assert expect["foreign_covered"] is True
    assert expect["covered"][-1][1] == 50.0
    assert expect["covered"][0][0] >= 0.0


def test_verify_gap_cli_writes_gap_and_profiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = tmp_path / "clip.geojson"
    clip.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [feature("at1", rect(10.0, 46.85, 10.5, 46.95))],
            }
        )
    )
    swiss = tmp_path / "swiss.json"
    swiss.write_text(json.dumps({"bbox": SWISS_BBOX}))
    points = tmp_path / "points.txt"
    points.write_text(
        "".join(
            f"P (CH/AT)\t{d} {10.0 + d * 1e-5} 46.9 0 0\n" for d in range(-50, 51, 5)
        )
    )
    values = tmp_path / "swiss.txt"
    values.write_text(
        "".join("1500.0\n" if d < 0 else "-8192.0\n" for d in range(-50, 51, 5))
    )

    status = main(
        [
            "verify-gap",
            "--clip", str(clip),
            "--area", str(FIXTURES / "area.geojson"),
            "--bbox", *map(str, ALPS),
            "--swiss", str(swiss),
            "--profile-points", str(points),
            "--profile-swiss", str(values),
        ]
    )  # fmt: skip

    document = json.loads(capsys.readouterr().out)
    assert status == 0
    assert document["gap"]["name"].startswith("gap z12/")
    assert document["profiles"]["P (CH/AT)"]["foreign_covered"] is True


# --- probe ------------------------------------------------------------------------


def test_probe_lists_what_sits_beside_attribution_json() -> None:
    bodies = {
        "https://m.example/download_urls.json": json.dumps(
            {
                "version": "2026-11-01",
                "items": [
                    {"name": "planet.pmtiles"},
                    {"name": "6-33-22.pmtiles"},
                    {"name": "coverage.gpkg", "url": "https://m.example/coverage.gpkg"},
                ],
            }
        ).encode(),
        "https://m.example/attribution.json": (
            FIXTURES / "attribution.json"
        ).read_bytes(),
    }

    lines = probe("https://m.example/", bodies.__getitem__)

    assert lines[0] == "release 2026-11-01"
    assert "2 pmtiles archives" in lines
    assert "other: coverage.gpkg https://m.example/coverage.gpkg" in lines
    assert "attribution.json: 5 sources" in lines
    assert any(line.startswith("  at1: ALS-DGM") for line in lines)


def test_probe_survives_an_unreadable_attribution() -> None:
    def fetch(url: str) -> bytes:
        if url.endswith("attribution.json"):
            raise OSError("blocked")
        return b'{"version": "v", "items": []}'

    lines = probe("https://m.example", fetch)

    assert lines[-1].startswith("attribution.json: unreadable")


def test_select_cli_writes_clip_and_credits(tmp_path: Path) -> None:
    clip, credits = tmp_path / "clip.geojson", tmp_path / "credits.json"

    status = main(
        [
            "select",
            "--coverage", str(FIXTURES / "coverage.geojson"),
            "--attribution", str(FIXTURES / "attribution.json"),
            "--area", str(FIXTURES / "area.geojson"),
            "--bbox", *map(str, ALPS),
            "--clip", str(clip),
            "--credits", str(credits),
        ]
    )  # fmt: skip

    assert status == 0
    assert len(json.loads(clip.read_text())["features"]) == 2
    assert json.loads(credits.read_text())["native_resolution_m"] == 5.0
