"""Tests for the relief raster's WebP twins."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from transcode_raster import is_stale, main, webp_path  # noqa: E402


def relief_tile(dist: Path, z: int = 0, x: int = 0, y: int = 0) -> Path:
    path = dist / "natural_earth" / "ne2sr" / str(z) / str(x) / f"{y}.png"
    path.parent.mkdir(parents=True)
    # A gradient rather than a flat fill, so the encoder has something to do.
    image = Image.new("RGB", (64, 64))
    image.putdata([(i % 64 * 4, i // 64 * 4, 128) for i in range(64 * 64)])
    image.save(path)
    return path


def test_writes_a_webp_twin_beside_each_png(tmp_path: Path) -> None:
    png = relief_tile(tmp_path)

    assert main(["--dist", str(tmp_path)]) == 0

    twin = webp_path(png)
    assert twin == png.parent / "0.webp"
    with Image.open(twin) as image:
        assert image.format == "WEBP"
        assert image.size == (64, 64)
    # The PNG stays: offline areas pinned before the switch still name it.
    assert png.exists()


def test_a_current_twin_is_not_re_encoded(tmp_path: Path) -> None:
    png = relief_tile(tmp_path)
    main(["--dist", str(tmp_path)])

    assert not is_stale(png)


def test_a_twin_older_than_its_png_is_stale(tmp_path: Path) -> None:
    png = relief_tile(tmp_path)
    main(["--dist", str(tmp_path)])
    twin = webp_path(png)
    os.utime(twin, (0, 0))

    assert is_stale(png)


def test_no_raster_tree_is_not_an_error(tmp_path: Path) -> None:
    # A style-only build skips the raster mirror (--skip-raster).
    assert main(["--dist", str(tmp_path)]) == 0
