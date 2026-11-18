"""Tests for scripts/config.sh (SNOW-485).

config.sh is shell, but the values it produces end up published in the style
JSON, where a malformed one is only visible as a map that renders nothing.
TILE_PATH in particular cannot use the `${VAR:=default}` form the other settings
use — it contains braces, and a parameter expansion ends at the first unescaped
`}`. Both the escaped and unescaped forms shipped broken values before this was
pinned down, so it is worth a test rather than a comment.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "scripts" / "config.sh"
sys.path.insert(0, str(CONFIG.parent))

from terrain_class import MAX_ZOOM, MIN_ZOOM  # noqa: E402

# Every name config.sh promises the other scripts, read off its own export
# lines rather than listed here, so a name added to one and not the other is
# still covered.
EXPORTED = re.findall(r"^export (.+)$", CONFIG.read_text(), re.MULTILINE)
EXPORTED = sorted({name for line in EXPORTED for name in line.split()})


def config_value(name: str, env: dict[str, str] | None = None) -> str:
    """Source config.sh in a subshell and echo one variable."""
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f'source "{CONFIG}"; printf "%s" "${name}"'],  # noqa: S607
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        check=True,
    )
    return result.stdout


def test_tile_path_has_no_stray_backslashes() -> None:
    # Escaping the braces round-tripped the backslashes into the style as
    # "/tiles/\{z}/\{x}/\{y}.mvt".
    assert "\\" not in config_value("TILE_PATH")


def test_origin_has_no_trailing_slash() -> None:
    # rewrite_style.py strips one, but a doubled slash in the default would
    # still show up anywhere else the value is interpolated.
    assert not config_value("TILES_ORIGIN").endswith("/")


def test_tile_path_carries_the_version() -> None:
    # The version is the whole point: without it a rebuilt archive reuses tile
    # URLs that are cached immutable for a year, and the swap is invisible.
    assert config_value("TILE_PATH") == "tiles/v1/{z}/{x}/{y}.mvt"


def test_tile_version_flows_into_tile_path() -> None:
    path = config_value("TILE_PATH", {"TILE_VERSION": "v9"})

    assert path == "tiles/v9/{z}/{x}/{y}.mvt"


@pytest.mark.parametrize("name", EXPORTED)
def test_exported_name_has_a_value(name: str) -> None:
    # An exported-but-never-assigned name is invisible here and fatal there: the
    # build scripts run under `set -u`, so the first one to dereference it dies
    # with "unbound variable" — which nobody finds until the next rebuild. All
    # four PLANETILER_* defaults were dropped this way, by a commit that only
    # meant to rewrite the hunk above them.
    assert config_value(name) != ""


def test_explicit_tile_path_still_wins() -> None:
    override = "custom/{z}/{x}/{y}.pbf"

    assert config_value("TILE_PATH", {"TILE_PATH": override}) == override


def test_terrain_tile_path_has_no_stray_backslashes() -> None:
    # Same brace hazard as TILE_PATH, same fix, and worth its own test because
    # the value ends up published in grid.json as the URL template another
    # repository resolves tiles with.
    assert "\\" not in config_value("TERRAIN_TILE_PATH")


def test_terrain_tile_path_carries_the_version() -> None:
    # v2 (SNOW-1505): the grid reaches beyond Switzerland.
    assert config_value("TERRAIN_TILE_PATH") == "terrain/v2/{x}/{y}.s16"


def test_terrain_class_version_follows_the_new_grid() -> None:
    # Classified from the v2 grid, so a class tile under the v2 URL would be a
    # Swiss-only answer cached for a year.
    assert config_value("TERRAIN_CLASS_VERSION") == "v3"


def test_mapterhorn_bbox_defaults_to_the_alps() -> None:
    parts = [float(part) for part in config_value("TERRAIN_MAPTERHORN_BBOX").split()]

    assert parts == [5.0, 43.7, 16.6, 48.4]


def test_an_empty_mapterhorn_bbox_stays_empty() -> None:
    # Empty is the switch back to the swissALTI3D-only v1 build. `:=` would
    # quietly replace it with the default and build v2 instead.
    assert (
        config_value("TERRAIN_MAPTERHORN_BBOX", {"TERRAIN_MAPTERHORN_BBOX": ""}) == ""
    )


def test_terrain_reads_a_named_dem_copy() -> None:
    assert config_value("TERRAIN_DEM_VERSION") == "2026-12"
    assert config_value("TERRAIN_DEM_VERSION", {"TERRAIN_DEM_VERSION": "x"}) == "x"


def test_mapterhorn_attribution_sits_beside_the_catalogue() -> None:
    value = config_value(
        "TERRAIN_MAPTERHORN_ATTRIBUTION", {"DEM_SOURCE_ORIGIN": "https://m.example"}
    )

    assert value == "https://m.example/attribution.json"


def test_swiss_comparison_reads_the_live_v1_grid() -> None:
    assert config_value("TERRAIN_REFERENCE_VERSION") == "v1"
    assert int(config_value("TERRAIN_COMPARE_SEED")) > 0


def test_terrain_version_flows_into_terrain_tile_path() -> None:
    path = config_value("TERRAIN_TILE_PATH", {"TERRAIN_VERSION": "v4"})

    assert path == "terrain/v4/{x}/{y}.s16"


def test_explicit_terrain_tile_path_still_wins() -> None:
    override = "terrain/custom/{x}/{y}.bin"

    value = config_value("TERRAIN_TILE_PATH", {"TERRAIN_TILE_PATH": override})

    assert value == override


def test_terrain_bbox_is_four_numbers() -> None:
    # Passed unquoted into fetch_swissalti3d.py --bbox, so it has to word-split
    # into exactly four values or argparse rejects it a long way downstream.
    parts = config_value("TERRAIN_BBOX").split()

    assert len(parts) == 4
    assert all(float(part) for part in parts)


def test_terrain_class_zooms_match_the_contract_and_the_worker() -> None:
    # Three statements of one range, in shell, Python and JavaScript. A build
    # wider than the Worker's range publishes tiles that are never served; one
    # narrower than tiles.json claims leaves MapLibre asking for 204s.
    worker = (CONFIG.parents[1] / "worker" / "src" / "tilesets.js").read_text()

    assert int(config_value("TERRAIN_CLASS_MIN_ZOOM")) == MIN_ZOOM
    assert int(config_value("TERRAIN_CLASS_MAX_ZOOM")) == MAX_ZOOM
    assert f"minZoom: {MIN_ZOOM}," in worker
    assert f"maxZoom: {MAX_ZOOM}," in worker


def test_dem_zooms_match_the_worker() -> None:
    # The elevation copy (SNOW-1444): build-dem.sh extracts the regional
    # archives to DEM_MAX_ZOOM and dem.js answers 204 past it without reading
    # R2. A build deeper than the Worker publishes tiles nobody can reach; one
    # shallower leaves the Worker opening archives for tiles they do not hold.
    worker = (CONFIG.parents[1] / "worker" / "src" / "dem.js").read_text()
    build = (CONFIG.parent / "build-dem.sh").read_text()
    max_zoom = int(config_value("DEM_MAX_ZOOM"))

    assert f"export const DEM_MAX_ZOOM = {max_zoom};" in worker
    # The planet archive's split, stated in the build and in the Worker.
    assert "export const DEM_PLANET_MAX_ZOOM = 12;" in worker
    assert "extract planet.pmtiles 12" in build
