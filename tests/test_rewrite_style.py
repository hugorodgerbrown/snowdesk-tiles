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


def test_sprite_and_glyphs_repointed_at_origin() -> None:
    result = rewrite(style(), ORIGIN, "alps.pmtiles")

    assert result["sprite"] == f"{ORIGIN}/sprites/ofm_f384/ofm"
    assert result["glyphs"] == f"{ORIGIN}/fonts/{{fontstack}}/{{range}}.pbf"


def test_vector_source_reads_local_pmtiles() -> None:
    result = rewrite(style(), ORIGIN, "alps.pmtiles")
    source = result["sources"]["openmaptiles"]

    assert source["url"] == f"pmtiles://{ORIGIN}/alps.pmtiles"
    # A leftover server-side tile endpoint would keep MapLibre talking to the
    # old origin even though the pmtiles:// url is present.
    assert "tiles" not in source


def test_trailing_slash_on_origin_is_dropped() -> None:
    result = rewrite(style(), f"{ORIGIN}/", "alps.pmtiles")

    assert result["sources"]["openmaptiles"]["url"] == (
        f"pmtiles://{ORIGIN}/alps.pmtiles"
    )


def test_missing_vector_source_is_an_error() -> None:
    without_vector = style()
    without_vector["sources"]["openmaptiles"]["type"] = "raster"

    with pytest.raises(ValueError, match="no vector source"):
        rewrite(without_vector, ORIGIN, "alps.pmtiles")
