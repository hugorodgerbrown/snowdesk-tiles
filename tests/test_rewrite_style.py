"""Tests for the style rewrite (SNOW-485)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrite_style import UPSTREAM_ORIGIN, rewrite  # noqa: E402

ORIGIN = "https://tiles.snowdesk-data.info"


def style() -> dict[str, Any]:
    return {
        "version": 8,
        "sprite": f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm",
        "glyphs": f"{UPSTREAM_ORIGIN}/fonts/{{fontstack}}/{{range}}.pbf",
        "sources": {
            "openmaptiles": {
                "type": "vector",
                "url": f"{UPSTREAM_ORIGIN}/planet",
                "tiles": [f"{UPSTREAM_ORIGIN}/planet/{{z}}/{{x}}/{{y}}.pbf"],
            }
        },
        "layers": [],
    }


TILE_PATH = "tiles/{z}/{x}/{y}.mvt"


def test_sprite_and_glyphs_repointed_at_origin() -> None:
    result = rewrite(style(), ORIGIN, TILE_PATH)

    assert result["sprite"] == f"{ORIGIN}/sprites/ofm_f384/ofm"
    assert result["glyphs"] == f"{ORIGIN}/fonts/{{fontstack}}/{{range}}.pbf"


def test_vector_source_uses_an_xyz_template() -> None:
    result = rewrite(style(), ORIGIN, TILE_PATH)
    source = result["sources"]["openmaptiles"]

    assert source["tiles"] == [f"{ORIGIN}/tiles/{{z}}/{{x}}/{{y}}.mvt"]
    # `url` and `tiles` together is ambiguous: MapLibre would fetch the former
    # as TileJSON to discover the latter, and the upstream one names the old
    # origin.
    assert "url" not in source


def test_vector_source_states_the_archive_zoom_range() -> None:
    # The dropped `url` used to carry this in its TileJSON. Without it MapLibre
    # defaults maxzoom to 22, requests z15+ tiles the archive does not hold, and
    # renders nothing above z14 rather than overzooming.
    result = rewrite(style(), ORIGIN, TILE_PATH)
    source = result["sources"]["openmaptiles"]

    assert source["minzoom"] == 0
    assert source["maxzoom"] == 14


def test_zoom_range_is_overridable() -> None:
    result = rewrite(style(), ORIGIN, TILE_PATH, min_zoom=4, max_zoom=12)
    source = result["sources"]["openmaptiles"]

    assert (source["minzoom"], source["maxzoom"]) == (4, 12)


def test_trailing_slash_on_origin_is_dropped() -> None:
    result = rewrite(style(), f"{ORIGIN}/", TILE_PATH)

    assert result["sources"]["openmaptiles"]["tiles"] == [
        f"{ORIGIN}/tiles/{{z}}/{{x}}/{{y}}.mvt"
    ]


def test_leading_slash_on_tile_path_does_not_double_up() -> None:
    result = rewrite(style(), ORIGIN, f"/{TILE_PATH}")

    assert result["sources"]["openmaptiles"]["tiles"] == [
        f"{ORIGIN}/tiles/{{z}}/{{x}}/{{y}}.mvt"
    ]


def test_missing_vector_source_is_an_error() -> None:
    without_vector = style()
    without_vector["sources"]["openmaptiles"]["type"] = "raster"

    with pytest.raises(ValueError, match="no vector source"):
        rewrite(without_vector, ORIGIN, TILE_PATH)
