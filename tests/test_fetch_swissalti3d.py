"""Tests for listing swissALTI3D squares (SNOW-908).

The download half of the fetch is shell; what is tested here is the part with
decisions in it — which asset of the four on each item to take, and which item
to take when a square has been flown more than once. Both are silent when wrong:
the wrong asset builds a grid at the wrong resolution, and the wrong item mixes
two survey vintages at an invisible seam.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_swissalti3d as fetch  # noqa: E402

BASE = "https://data.geo.admin.ch/ch.swisstopo.swissalti3d"


def item(
    square: str, year: int = 2019, gsds: tuple[float, ...] = (0.5, 2.0)
) -> dict[str, Any]:
    """Build a STAC item shaped like the ones data.geo.admin.ch serves."""
    name = f"swissalti3d_{year}_{square}"
    assets: dict[str, Any] = {}
    for gsd in gsds:
        stem = f"{name}_{gsd:g}_2056_5728"
        assets[f"{stem}.tif"] = {"href": f"{BASE}/{name}/{stem}.tif", "gsd": gsd}
        # The same ground is also published as zipped ASCII XYZ, at the same
        # gsd — so selecting on resolution alone is not enough.
        assets[f"{stem}.xyz.zip"] = {
            "href": f"{BASE}/{name}/{stem}.xyz.zip",
            "gsd": gsd,
        }
    east, north = (int(part) for part in square.split("-"))
    return {
        "id": name,
        "bbox": [east / 1000, north / 1000, east / 1000 + 0.01, north / 1000 + 0.01],
        "assets": assets,
    }


# --- Choosing an asset ------------------------------------------------------


def test_the_two_metre_geotiff_is_chosen() -> None:
    href = fetch.asset_href(item("2608-1094"), 2.0)

    assert href is not None
    assert href.endswith("_2_2056_5728.tif")


def test_the_xyz_archive_is_never_chosen() -> None:
    # It carries the same gsd, so anything selecting on resolution alone picks
    # it roughly half the time depending on dict ordering.
    assert ".zip" not in (fetch.asset_href(item("2608-1094"), 2.0) or "")
    assert ".zip" not in (fetch.asset_href(item("2608-1094"), 0.5) or "")


def test_an_item_without_the_requested_resolution_returns_nothing() -> None:
    assert fetch.asset_href(item("2608-1094", gsds=(0.5,)), 2.0) is None


# --- Choosing an item -------------------------------------------------------


def test_one_item_per_square() -> None:
    chosen = fetch.select([item("2608-1094"), item("2608-1095")], 2.0)

    assert len(chosen) == 2
    assert {fetch.square_of(entry) for entry in chosen} == {"2608-1094", "2608-1095"}


def test_a_resurveyed_square_keeps_the_newer_flight() -> None:
    # swissALTI3D is re-flown on a six-year cycle. Every item published today is
    # 2019, so this cannot be observed against the live API — and that is
    # precisely why it is worth pinning: the first rebuild after a revision
    # would otherwise blend two vintages along whatever line the re-survey
    # happened to stop at.
    chosen = fetch.select(
        [item("2608-1094", year=2019), item("2608-1094", year=2025)], 2.0
    )

    assert len(chosen) == 1
    assert fetch.survey_year(chosen[0]) == 2025
    assert "2025" in chosen[0]["_href"]


def test_an_item_with_no_usable_asset_is_dropped_not_fatal() -> None:
    chosen = fetch.select([item("2608-1094"), item("2608-1095", gsds=(0.5,))], 2.0)

    assert [fetch.square_of(entry) for entry in chosen] == ["2608-1094"]


def test_an_unexpected_item_id_is_refused() -> None:
    # The id is parsed for both the square and the survey year, so a change in
    # its shape has to stop the build rather than silently defeat the dedupe.
    with pytest.raises(ValueError, match="unexpected"):
        fetch.square_of({"id": "swissalti3d-2608-1094"})


# --- The manifest -----------------------------------------------------------


def test_the_manifest_bbox_is_the_union_of_what_was_selected() -> None:
    # Not the box that was asked for. Coverage stops at the national border, and
    # the build snaps its warp extent to this — asking for the whole Alps and
    # snapping that would warp a wide margin of nothing.
    chosen = fetch.select([item("2600-1100"), item("2700-1200")], 2.0)
    summary = fetch.manifest(chosen, 2.0)

    assert summary["bbox"] == [2.6, 1.1, 2.71, 1.21]
    assert summary["count"] == 2
    assert summary["survey_years"] == [2019]
    assert summary["gsd"] == 2.0


# --- Paging -----------------------------------------------------------------


def test_paging_follows_next_until_it_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "start": {
            "features": [item("2600-1100")],
            "links": [{"rel": "next", "href": "page2"}],
        },
        "page2": {"features": [item("2600-1101")], "links": []},
    }
    monkeypatch.setattr(
        fetch, "fetch_json", lambda url: pages["start" if "items?" in url else url]
    )

    found = fetch.items((6.0, 46.0, 6.1, 46.1))

    assert [entry["id"] for entry in found] == [
        "swissalti3d_2019_2600-1100",
        "swissalti3d_2019_2600-1101",
    ]


def test_a_paging_loop_is_an_error_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetch,
        "fetch_json",
        lambda url: {"features": [], "links": [{"rel": "next", "href": url}]},
    )

    with pytest.raises(RuntimeError, match="looped"):
        fetch.items((6.0, 46.0, 6.1, 46.1))


def test_a_non_http_url_is_refused() -> None:
    with pytest.raises(ValueError, match="non-HTTP"):
        fetch.fetch_json("file:///etc/passwd")
