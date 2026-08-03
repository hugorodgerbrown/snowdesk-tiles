"""Tests for scripts/config.sh (SNOW-485).

config.sh is shell, but the values it produces end up published in the style
JSON, where a malformed one is only visible as a map that renders nothing.
TILE_PATH in particular cannot use the `${VAR:=default}` form the other settings
use — it contains braces, and a parameter expansion ends at the first unescaped
`}`. Both the escaped and unescaped forms shipped broken values before this was
pinned down, so it is worth a test rather than a comment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "scripts" / "config.sh"


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


def test_explicit_tile_path_still_wins() -> None:
    override = "custom/{z}/{x}/{y}.pbf"

    assert config_value("TILE_PATH", {"TILE_PATH": override}) == override
