"""Tests for the sprite/glyph mirror (SNOW-485)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mirror_assets import (  # noqa: E402
    UPSTREAM_ORIGIN,
    font_stacks,
    glyph_urls,
    local_path,
    raster_tile_urls,
    sprite_urls,
)


def test_string_sprite_expands_to_the_1x_and_2x_pair() -> None:
    style: dict[str, Any] = {"sprite": f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm"}

    assert sprite_urls(style) == [
        f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm.json",
        f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm.png",
        f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm@2x.json",
        f"{UPSTREAM_ORIGIN}/sprites/ofm_f384/ofm@2x.png",
    ]


def test_list_sprite_form_is_supported() -> None:
    style: dict[str, Any] = {
        "sprite": [{"id": "default", "url": f"{UPSTREAM_ORIGIN}/sprites/a/ofm"}]
    }

    assert sprite_urls(style)[0] == f"{UPSTREAM_ORIGIN}/sprites/a/ofm.json"


def test_font_stacks_are_comma_joined_like_maplibre_builds_them() -> None:
    style: dict[str, Any] = {
        "layers": [
            {"id": "a", "layout": {"text-font": ["Noto Sans Regular"]}},
            {"id": "b", "layout": {"text-font": ["Noto Sans Bold", "Noto Sans"]}},
            {"id": "c", "layout": {}},
        ]
    }

    assert font_stacks(style) == ["Noto Sans Bold,Noto Sans", "Noto Sans Regular"]


def test_computed_text_font_is_skipped_not_crashed_on() -> None:
    style: dict[str, Any] = {
        "layers": [{"id": "a", "layout": {"text-font": ["get", "font"]}}]
    }

    # A ["get", ...] expression is a list of strings and cannot be told apart
    # from a literal stack; the point is that it does not raise.
    assert font_stacks(style) == ["get,font"]


def test_glyph_urls_percent_encode_the_fontstack() -> None:
    style: dict[str, Any] = {
        "glyphs": f"{UPSTREAM_ORIGIN}/fonts/{{fontstack}}/{{range}}.pbf",
        "layers": [{"id": "a", "layout": {"text-font": ["Noto Sans Regular"]}}],
    }

    urls = glyph_urls(style, max_codepoint=511)

    assert urls == [
        f"{UPSTREAM_ORIGIN}/fonts/Noto%20Sans%20Regular/0-255.pbf",
        f"{UPSTREAM_ORIGIN}/fonts/Noto%20Sans%20Regular/256-511.pbf",
    ]


def test_raster_pyramid_is_enumerated_to_maxzoom() -> None:
    style: dict[str, Any] = {
        "sources": {
            "ne2_shaded": {
                "type": "raster",
                "maxzoom": 2,
                "tiles": [
                    f"{UPSTREAM_ORIGIN}/natural_earth/ne2sr/{{z}}/{{x}}/{{y}}.png"
                ],
            }
        }
    }

    urls = raster_tile_urls(style)

    # 4^0 + 4^1 + 4^2
    assert len(urls) == 21
    assert f"{UPSTREAM_ORIGIN}/natural_earth/ne2sr/0/0/0.png" in urls
    assert f"{UPSTREAM_ORIGIN}/natural_earth/ne2sr/2/3/3.png" in urls


def test_vector_sources_are_not_treated_as_raster_pyramids() -> None:
    style: dict[str, Any] = {
        "sources": {"openmaptiles": {"type": "vector", "tiles": ["https://x/{z}.pbf"]}}
    }

    assert raster_tile_urls(style) == []


def test_absurd_maxzoom_is_clamped_rather_than_crawled() -> None:
    style: dict[str, Any] = {
        "sources": {
            "deep": {
                "type": "raster",
                "maxzoom": 14,
                "tiles": ["https://x/{z}/{x}/{y}.png"],
            }
        }
    }

    # Clamped to RASTER_MAXZOOM_LIMIT (8): sum of 4^0..4^8, not 4^0..4^14.
    assert len(raster_tile_urls(style)) == sum(4**z for z in range(9))


def test_local_path_decodes_so_it_matches_the_object_key() -> None:
    url = f"{UPSTREAM_ORIGIN}/fonts/Noto%20Sans%20Regular/0-255.pbf"

    assert local_path(url, Path("dist")) == Path(
        "dist/fonts/Noto Sans Regular/0-255.pbf"
    )
