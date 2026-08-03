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


def test_tile_path_keeps_its_braces() -> None:
    assert config_value("TILE_PATH") == "tiles/{z}/{x}/{y}.mvt"


def test_tile_path_has_no_stray_backslashes() -> None:
    # Escaping the braces round-tripped the backslashes into the style as
    # "/tiles/\{z}/\{x}/\{y}.mvt".
    assert "\\" not in config_value("TILE_PATH")


def test_tile_path_can_be_overridden() -> None:
    override = "vector/{z}/{x}/{y}.pbf"

    assert config_value("TILE_PATH", {"TILE_PATH": override}) == override


def test_origin_has_no_trailing_slash() -> None:
    # rewrite_style.py strips one, but a doubled slash in the default would
    # still show up anywhere else the value is interpolated.
    assert not config_value("TILES_ORIGIN").endswith("/")
