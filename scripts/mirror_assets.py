#!/usr/bin/env python3
"""Mirror the OpenFreeMap Liberty sprites and glyphs into the local dist tree.

SNOW-485. Self-hosting the Liberty basemap means serving copies of everything
the style references. The vector tiles come from planetiler (see
``build-extract.sh``); the two remaining asset classes — the sprite sheet and
the Noto Sans glyph PBFs — are small, immutable, and mirrored from upstream by
this script.

The mirror is keyed on the *URL path*, not on a hand-written layout. Whatever
path the upstream style references becomes the local path, and therefore the R2
object key. That is what makes ``rewrite_style.py``'s blanket origin swap
sufficient: only the host changes, every path stays byte-identical.

Glyph ranges that upstream does not publish (a fontstack with no glyphs in that
Unicode block) return 404 and are skipped — that is expected, not an error.

Standalone operational tooling: stdlib only, no Django.

Usage:
    python scripts/mirror_assets.py --dist dist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

UPSTREAM_ORIGIN = os.environ.get("UPSTREAM_ORIGIN", "https://tiles.openfreemap.org")
UPSTREAM_STYLE_URL = os.environ.get(
    "UPSTREAM_STYLE_URL", f"{UPSTREAM_ORIGIN}/styles/liberty"
)

# Glyph PBFs are published in 256-codepoint blocks across the Basic Multilingual
# Plane. Mirroring all of them is ~1500 small files; the 404s cost one HEAD-ish
# round trip each and keep us honest about which blocks actually exist.
GLYPH_BLOCK = 256
GLYPH_MAX_CODEPOINT = 65535

# Sprite sheets are published as a 1x and a 2x pair, each a JSON index plus a
# PNG atlas.
SPRITE_SUFFIXES = (".json", ".png", "@2x.json", "@2x.png")

# Raster pyramids grow as 4^z. Liberty's shaded relief stops at z6 (~5.5k
# tiles); anything materially deeper is a mistake, not a mirror job.
RASTER_MAXZOOM_LIMIT = 8


def fetch(url: str) -> bytes:
    """Return the body at ``url``, or raise ``urllib.error.HTTPError``."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - scheme checked
        body: bytes = response.read()
    return body


def load_style(source: str) -> dict[str, Any]:
    """Return the style JSON, fetched over HTTP or read from a local path."""
    raw = (
        fetch(source).decode("utf-8")
        if source.startswith(("http://", "https://"))
        else Path(source).read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def sprite_urls(style: dict[str, Any]) -> list[str]:
    """Return every sprite file URL the style references.

    The style spec allows ``sprite`` to be a single base URL string or a list of
    ``{"id": ..., "url": ...}`` entries. Each base expands to the 1x/2x JSON and
    PNG pair.
    """
    sprite = style.get("sprite")
    if not sprite:
        return []

    bases = (
        [sprite]
        if isinstance(sprite, str)
        else [entry["url"] for entry in sprite if "url" in entry]
    )
    return [f"{base}{suffix}" for base in bases for suffix in SPRITE_SUFFIXES]


def font_stacks(style: dict[str, Any]) -> list[str]:
    """Return the ``{fontstack}`` values MapLibre will request for this style.

    A layer's ``text-font`` is a list of font names; MapLibre joins them with a
    comma to build the glyph URL. Data-driven ``text-font`` expressions cannot
    be resolved statically — they are reported so the operator can mirror the
    affected stacks by hand rather than silently shipping a style with missing
    glyphs.
    """
    stacks: set[str] = set()
    for layer in style.get("layers", []):
        fonts = layer.get("layout", {}).get("text-font")
        if fonts is None:
            continue
        if isinstance(fonts, list) and all(isinstance(name, str) for name in fonts):
            stacks.add(",".join(fonts))
        else:
            sys.stderr.write(
                f"WARNING: layer {layer.get('id')!r} uses a computed text-font; "
                "mirror its glyph ranges manually\n"
            )
    return sorted(stacks)


def glyph_urls(style: dict[str, Any], max_codepoint: int) -> list[str]:
    """Return every glyph PBF URL implied by the style's fontstacks."""
    template = style.get("glyphs")
    if not template:
        return []

    urls = []
    for stack in font_stacks(style):
        for start in range(0, max_codepoint + 1, GLYPH_BLOCK):
            end = start + GLYPH_BLOCK - 1
            urls.append(
                template.replace("{fontstack}", urllib.parse.quote(stack)).replace(
                    "{range}", f"{start}-{end}"
                )
            )
    return urls


def raster_tile_urls(style: dict[str, Any]) -> list[str]:
    """Return every raster tile URL the style references.

    Liberty layers a Natural Earth shaded-relief raster under the vector data at
    low zooms (``ne2_shaded``, maxzoom 6). It is a separate tile pyramid from
    the PMTiles archive and has to be mirrored tile by tile, or the basemap
    404s when zoomed out. 4^z tiles per level, so a maxzoom of 6 is ~5.5k small
    PNGs; the ``maxzoom`` guard below keeps a style change from silently
    turning this into a million-request crawl.
    """
    urls = []
    for name, spec in style.get("sources", {}).items():
        if spec.get("type") != "raster" or "tiles" not in spec:
            continue

        maxzoom = int(spec.get("maxzoom", 0))
        if maxzoom > RASTER_MAXZOOM_LIMIT:
            sys.stderr.write(
                f"WARNING: raster source {name!r} has maxzoom {maxzoom}; "
                f"refusing to enumerate beyond {RASTER_MAXZOOM_LIMIT}\n"
            )
            maxzoom = RASTER_MAXZOOM_LIMIT

        for template in spec["tiles"]:
            for zoom in range(int(spec.get("minzoom", 0)), maxzoom + 1):
                for x in range(2**zoom):
                    for y in range(2**zoom):
                        urls.append(
                            template.replace("{z}", str(zoom))
                            .replace("{x}", str(x))
                            .replace("{y}", str(y))
                        )
    return urls


def local_path(url: str, dist: Path) -> Path:
    """Map an upstream asset URL to its path in the local dist tree.

    The URL path is percent-decoded, so ``fonts/Noto%20Sans%20Regular/0-255.pbf``
    lands at ``fonts/Noto Sans Regular/0-255.pbf``. Object storage keys are the
    decoded form — the HTTP layer decodes before key lookup — so decoding here
    keeps the local tree a faithful preimage of the bucket.
    """
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lstrip("/")
    return dist / path


def download(url: str, dist: Path, *, optional: bool = False) -> bool:
    """Fetch ``url`` into the dist tree. Return whether anything was written."""
    target = local_path(url, dist)
    if target.exists():
        return False

    try:
        body = fetch(url)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return False
        raise

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return True


def mirror(urls: list[str], dist: Path, *, optional: bool, workers: int) -> int:
    """Download ``urls`` concurrently. Return the count actually written."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda url: download(url, dist, optional=optional), urls)
        return sum(results)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — mirror sprites and glyphs into the dist tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=UPSTREAM_STYLE_URL,
        help=f"style URL or local path (default: {UPSTREAM_STYLE_URL})",
    )
    parser.add_argument(
        "--dist",
        default=os.environ.get("DIST_DIR", "dist"),
        help="local staging tree to write into (default: dist)",
    )
    parser.add_argument(
        "--max-codepoint",
        type=int,
        default=GLYPH_MAX_CODEPOINT,
        help=(
            "highest Unicode codepoint to mirror glyphs for "
            f"(default: {GLYPH_MAX_CODEPOINT}; lower it to skip CJK blocks)"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="concurrent downloads (default: 8)",
    )
    parser.add_argument(
        "--skip-raster",
        action="store_true",
        help="skip the Natural Earth raster pyramid (the slowest part)",
    )
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    style = load_style(args.source)

    sprites = sprite_urls(style)
    sys.stderr.write(f"mirroring {len(sprites)} sprite file(s)\n")
    written = mirror(sprites, dist, optional=True, workers=args.workers)

    glyphs = glyph_urls(style, args.max_codepoint)
    sys.stderr.write(
        f"mirroring {len(glyphs)} glyph range(s) across "
        f"{len(font_stacks(style))} fontstack(s); 404s are skipped\n"
    )
    written += mirror(glyphs, dist, optional=True, workers=args.workers)

    if args.skip_raster:
        sys.stderr.write("skipping raster tiles (--skip-raster)\n")
    else:
        rasters = raster_tile_urls(style)
        sys.stderr.write(f"mirroring {len(rasters)} raster tile(s)\n")
        written += mirror(rasters, dist, optional=True, workers=args.workers)

    sys.stderr.write(f"wrote {written} new file(s) under {dist}/\n")
    if not written and not any(dist.glob("**/*")):
        sys.stderr.write("ERROR: nothing mirrored — check the upstream style\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
