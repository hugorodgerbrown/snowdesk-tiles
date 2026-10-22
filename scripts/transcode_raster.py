#!/usr/bin/env python3
"""Write a WebP twin beside every mirrored Natural Earth relief PNG.

Upstream publishes Liberty's shaded-relief underlay as 512 px RGB PNGs, and
lossless PNG is the wrong container for a continuous-tone photograph of the
ground: a single z6 tile over the Alps is 190-325 KB. The Snowdesk map's first
view needs three of them, about 700 KB, which was 40% of the whole mobile page
weight on /map. The same tiles as lossy WebP at quality 80 are 20-25 KB each,
and the relief reads the same at the size the map draws it.

The PNGs stay in the tree and in the bucket. The style that names them has a
one-hour TTL, and a Snowdesk device that pinned an offline area before the
switch holds PNG URLs in its own cache, so the old paths must keep answering.
``rewrite_style.py`` points the style at the ``.webp`` twins.

Needs Pillow — the only script in the publish path that does. ``build.sh``
checks for it before calling this.

Usage:
    python scripts/transcode_raster.py --dist dist
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The mirrored pyramid's root under the dist tree, matching the upstream URL path
# (see ``mirror_assets.local_path``).
RASTER_ROOT = "natural_earth"

# Measured on the z6 Alpine tiles: 80 is where banding in the valley shading
# stops being visible at the map's 2x tile density; 70 saves another 3 KB a
# tile and starts to show it.
DEFAULT_QUALITY = 80


def webp_path(png: Path) -> Path:
    """Return the path of the WebP twin for ``png``."""
    return png.with_suffix(".webp")


def is_stale(png: Path) -> bool:
    """Whether ``png`` has no WebP twin, or one older than itself."""
    twin = webp_path(png)
    return not twin.exists() or twin.stat().st_mtime < png.stat().st_mtime


def transcode(png: Path, quality: int) -> None:
    """Encode ``png`` as lossy WebP beside it."""
    from PIL import Image  # deferred so --help works without Pillow installed

    with Image.open(png) as image:
        # The relief is opaque RGB. An alpha channel, if upstream ever adds
        # one, would be dropped here; WebP would carry it, but nothing draws
        # through this layer so it would only cost bytes.
        image.convert("RGB").save(webp_path(png), "WEBP", quality=quality, method=6)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — transcode every stale relief PNG under the dist tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        default=os.environ.get("DIST_DIR", "dist"),
        help="local staging tree to read and write (default: dist)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        help=f"WebP quality, 0-100 (default: {DEFAULT_QUALITY})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="concurrent encodes (default: one per CPU)",
    )
    args = parser.parse_args(argv)

    root = Path(args.dist) / RASTER_ROOT
    if not root.is_dir():
        sys.stderr.write(f"no {root}/ — nothing to transcode\n")
        return 0

    pngs = [png for png in sorted(root.rglob("*.png")) if is_stale(png)]
    sys.stderr.write(f"transcoding {len(pngs)} relief tile(s) to WebP\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda png: transcode(png, args.quality), pngs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
