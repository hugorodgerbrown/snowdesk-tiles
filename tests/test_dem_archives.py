"""Tests for scripts/dem_archives.py (SNOW-1444).

What is pinned: only the regional archives the supported area overlaps are
picked (not the planet archive, not one merely touching an edge, not one in the
gap between two parts of the area); the manifest names every archive with its
source checksum and the area's hash; and locate agrees with the Worker's tile
arithmetic at Zermatt. The committed area file is also checked to select the
Alpine archives it was built for.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dem_archives  # noqa: E402

AREA_FILE = SCRIPTS.parent / "dem" / "supported-area.geojson"


def box(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """Return a Polygon geometry covering one lon/lat box."""
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


def item(
    name: str,
    bounds: tuple[float, float, float, float],
    min_zoom: int = 13,
    md5: str = "0" * 32,
) -> dict[str, Any]:
    """One download_urls.json entry."""
    west, south, east, north = bounds
    return {
        "name": name,
        "url": f"https://download.mapterhorn.com/{name}",
        "md5sum": md5,
        "min_lon": west,
        "min_lat": south,
        "max_lon": east,
        "max_lat": north,
        "min_zoom": min_zoom,
        "max_zoom": 17,
    }


CATALOGUE = {
    "version": "0.0.13",
    "items": [
        item("planet.pmtiles", (-180, -85, 180, 85), min_zoom=0, md5="p" * 32),
        item("6-33-22.pmtiles", (5.625, 45.08, 11.25, 48.92), md5="a" * 32),
        item("6-34-22.pmtiles", (11.25, 45.08, 16.875, 48.92), md5="b" * 32),
        item("6-40-22.pmtiles", (45.0, 45.08, 50.625, 48.92)),
    ],
}


def test_select_picks_the_overlapping_regional_archives_only() -> None:
    area = {"type": "Feature", "geometry": box(7.0, 45.5, 12.0, 47.0)}

    assert dem_archives.select(area, CATALOGUE) == [
        "6-33-22.pmtiles",
        "6-34-22.pmtiles",
    ]


def test_an_archive_that_only_touches_the_area_is_left_out() -> None:
    # The area ends on 11.25°E, the edge between two z6 tiles.
    area = box(7.0, 45.5, 11.25, 47.0)

    assert dem_archives.select(area, CATALOGUE) == ["6-33-22.pmtiles"]


def test_the_gap_between_two_parts_of_the_area_is_left_out() -> None:
    # One box round both parts would take 6-34-22 too.
    area = {
        "type": "MultiPolygon",
        "coordinates": [
            box(7.0, 45.5, 8.0, 46.5)["coordinates"],
            box(20.0, 45.5, 21.0, 46.5)["coordinates"],
        ],
    }
    catalogue = {
        "items": [
            *CATALOGUE["items"],
            item("6-35-22.pmtiles", (16.875, 45.08, 22.5, 48.92)),
        ]
    }

    assert dem_archives.select(area, catalogue) == [
        "6-33-22.pmtiles",
        "6-35-22.pmtiles",
    ]


def test_an_area_with_no_polygons_is_refused() -> None:
    with pytest.raises(ValueError, match="no polygons"):
        dem_archives.area_boxes({"type": "FeatureCollection", "features": []})
    with pytest.raises(ValueError, match="unsupported"):
        dem_archives.area_boxes({"type": "Point", "coordinates": [7.0, 46.0]})


def test_manifest_names_every_archive_and_the_area() -> None:
    area = b'{"type": "Polygon"}'

    document = dem_archives.manifest(
        "2026-11", area, CATALOGUE, ["6-33-22.pmtiles"], 15
    )

    assert document["version"] == "2026-11"
    assert document["source_release"] == "0.0.13"
    assert document["max_zoom"] == 15
    assert document["area_sha256"] == hashlib.sha256(area).hexdigest()
    assert document["archives"] == [
        {"name": "planet.pmtiles", "source_md5": "p" * 32},
        {"name": "6-33-22.pmtiles", "source_md5": "a" * 32},
    ]
    assert document["built_at"].endswith("Z")


def test_manifest_refuses_an_archive_the_catalogue_does_not_list() -> None:
    with pytest.raises(ValueError, match="6-99-99.pmtiles"):
        dem_archives.manifest("2026-11", b"", CATALOGUE, ["6-99-99.pmtiles"], 15)


@pytest.mark.parametrize(
    ("z", "expected"),
    [(12, (2136, 1456)), (13, (4272, 2913)), (15, (17089, 11654)), (6, (33, 22))],
)
def test_locate_agrees_with_the_worker_at_zermatt(
    z: int, expected: tuple[int, int]
) -> None:
    # The same tiles worker/test/dem.test.js routes.
    assert dem_archives.locate(7.7491, 46.0207, z) == expected


def test_the_committed_area_selects_the_alpine_archives() -> None:
    # Mapterhorn 0.0.13's z6 grid: the Alps, the Pyrenees and Corsica.
    area = json.loads(AREA_FILE.read_text())
    lon_step = 360 / 64
    catalogue: dict[str, Any] = {"items": []}
    for x in range(28, 38):
        for y in range(20, 26):
            west = -180 + x * lon_step
            catalogue["items"].append(
                item(
                    f"6-{x}-{y}.pmtiles", (west, _lat(y + 1), west + lon_step, _lat(y))
                )
            )

    assert dem_archives.select(area, catalogue) == [
        "6-31-23.pmtiles",
        "6-32-22.pmtiles",
        "6-32-23.pmtiles",
        "6-33-22.pmtiles",
        "6-33-23.pmtiles",
        "6-34-22.pmtiles",
        "6-34-23.pmtiles",
    ]


def _lat(y: int) -> float:
    """Return the latitude of the top edge of z6 row y."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 64))))


def test_main_prints_one_line_per_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalogue = tmp_path / "download_urls.json"
    catalogue.write_text(json.dumps(CATALOGUE))
    area = tmp_path / "area.geojson"
    area.write_text(json.dumps(box(7.0, 45.5, 8.0, 46.5)))

    dem_archives.main(["select", str(area), str(catalogue)])
    dem_archives.main(["locate", "7.7491", "46.0207", "12"])
    dem_archives.main(
        [
            "manifest",
            "2026-11",
            str(area),
            str(catalogue),
            "6-33-22.pmtiles",
            "--max-zoom",
            "15",
        ]
    )

    out = capsys.readouterr().out
    assert out.startswith("6-33-22.pmtiles\n12 2136 1456\n{")
    assert json.loads(out.split("\n", 2)[2])["version"] == "2026-11"
