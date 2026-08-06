"""Tests for the style rewrite (SNOW-485)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrite_style import (  # noqa: E402
    DEFAULT_ATTRIBUTION,
    RASTER_ATTRIBUTION,
    UPSTREAM_ORIGIN,
    rewrite,
)

ORIGIN = "https://tiles.snowdesk-data.info"

WORKER_SOURCE = Path(__file__).resolve().parents[1] / "worker" / "src" / "index.js"


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
            },
            # Liberty's shaded-relief underlay. Upstream carries no attribution
            # on it either.
            "natural_earth_shaded_relief": {
                "type": "raster",
                "maxzoom": 6,
                "tiles": [
                    f"{UPSTREAM_ORIGIN}/natural_earth/ne2sr/{{z}}/{{x}}/{{y}}.png"
                ],
            },
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


def test_vector_source_carries_an_attribution() -> None:
    # Lost the same way the zoom range was: the dropped `url` carried it in
    # TileJSON. Without it the Snowdesk legend, which reads `attribution` off
    # each runtime source, renders "Map data" as a bare heading and the required
    # OSM credit never appears (SNOW-640).
    result = rewrite(style(), ORIGIN, TILE_PATH)
    source = result["sources"]["openmaptiles"]

    assert source["attribution"] == DEFAULT_ATTRIBUTION
    assert "openstreetmap.org/copyright" in source["attribution"]


def test_attribution_is_overridable() -> None:
    result = rewrite(style(), ORIGIN, TILE_PATH, attribution="&copy; Somebody")

    assert result["sources"]["openmaptiles"]["attribution"] == "&copy; Somebody"


def test_raster_source_carries_an_attribution() -> None:
    result = rewrite(style(), ORIGIN, TILE_PATH)

    assert (
        result["sources"]["natural_earth_shaded_relief"]["attribution"]
        == RASTER_ATTRIBUTION
    )


def test_an_existing_attribution_is_left_alone() -> None:
    upstream = style()
    upstream["sources"]["natural_earth_shaded_relief"]["attribution"] = "&copy; Theirs"

    result = rewrite(upstream, ORIGIN, TILE_PATH)

    assert (
        result["sources"]["natural_earth_shaded_relief"]["attribution"]
        == "&copy; Theirs"
    )


def test_attribution_matches_the_workers_tilejson() -> None:
    # The style and the Worker's TileJSON both publish this credit, and a client
    # can reach either. verify.sh compares the two live; this catches a drift
    # before it is deployed. The Worker builds the string by concatenating
    # single-quoted JS literals, so reassemble it the same way.
    body = re.search(r"attribution:\s*(.*?),\n", WORKER_SOURCE.read_text(), re.S)
    assert body is not None, "no attribution found in worker/src/index.js"

    assert "".join(re.findall(r"'([^']*)'", body.group(1))) == DEFAULT_ATTRIBUTION


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
