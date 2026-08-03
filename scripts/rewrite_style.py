#!/usr/bin/env python3
"""Rewrite the OpenFreeMap Liberty style JSON for the self-hosted origin.

SNOW-485. The public Liberty style at ``tiles.openfreemap.org`` references its
own host in three places — the vector ``sources`` URL, the ``sprite`` base, and
the ``glyphs`` template. Self-hosting means serving a copy of that style whose
internal URLs point back at ``tiles.snowdesk-data.info`` instead, and whose
vector source reads XYZ tiles from the Worker (see ``worker/``) rather than the
OpenFreeMap tile server.

Standalone operational tooling: stdlib only, no Django. Run it when building or
refreshing the published assets; write the output to ``dist/styles/liberty``.

Usage (ORIGIN = https://tiles.snowdesk-data.info):
    python scripts/rewrite_style.py --origin ORIGIN > dist/styles/liberty

    # Rewrite a local copy instead of fetching the upstream style:
    python scripts/rewrite_style.py --source ./liberty.json --origin ORIGIN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

UPSTREAM_ORIGIN = os.environ.get("UPSTREAM_ORIGIN", "https://tiles.openfreemap.org")
UPSTREAM_STYLE_URL = os.environ.get(
    "UPSTREAM_STYLE_URL", f"{UPSTREAM_ORIGIN}/styles/liberty"
)

# The zoom range the archive actually holds. Must be written into the style
# explicitly — see ``rewrite``'s docstring for why dropping ``url`` makes these
# mandatory. Defaults match what ``build-extract.sh`` produces (planetiler's
# own default maximum); the archive's TileJSON, which the Worker derives from
# the PMTiles header, is the authority to check them against:
#
#     curl -s https://tiles.snowdesk-data.info/tiles/v1/tiles.json
DEFAULT_MIN_ZOOM = 0
DEFAULT_MAX_ZOOM = 14


def load_style(source: str) -> dict[str, Any]:
    """Return the style JSON, fetched over HTTP or read from a local path."""
    if source.startswith(("http://", "https://")):
        # OpenFreeMap rejects requests with no User-Agent (403), so send one.
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            source, headers={"User-Agent": "snowdesk-tiles"}
        )
        with urllib.request.urlopen(request) as response:  # noqa: S310 - checked
            loaded: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return loaded
    with open(source, encoding="utf-8") as handle:
        from_file: dict[str, Any] = json.load(handle)
    return from_file


def rewrite(
    style: dict[str, Any],
    origin: str,
    tile_path: str,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
) -> dict[str, Any]:
    """Repoint every upstream URL at ``origin`` and tiles at ``tile_path``.

    Every ``tiles.openfreemap.org`` occurrence (sprite, glyphs, and any stray
    reference) is swapped for ``origin``; each vector source is rewritten to an
    XYZ ``tiles`` template served by the Worker, carrying the archive's own
    ``[min_zoom, max_zoom]`` range.

    An earlier revision emitted ``pmtiles://`` here so MapLibre could range-read
    the archive directly. That needs the client to register the PMTiles protocol,
    and it hands the frontend a source with no tile URLs — which the Snowdesk map
    cannot work with: SNOW-521 resolves each basemap's vector-tile URL template
    and SNOW-484's service worker pins basemap URLs for offline use, and neither
    can express range reads into one 1.5 GB object. A plain ``tiles`` array keeps
    this style shaped like every other basemap in the catalogue.

    Writing the zoom range is not optional, and is the whole reason these
    arguments exist. Upstream carries it in the TileJSON that ``url`` points at,
    so dropping ``url`` drops it too — and a vector source with no ``maxzoom``
    defaults to 22 in MapLibre. The client then requests z15+ tiles the archive
    does not hold (the Worker answers 204) instead of overzooming z14, so every
    basemap layer vanishes above z14: no roads, no labels, no terrain. That also
    breaks Snowdesk's offline area downloads, which pin z10-14 and rely on
    overzoom for anything closer in.

    Raises if no vector source is found — that would mean the upstream style
    shape changed and the rewrite is no longer safe to trust.
    """
    origin = origin.rstrip("/")

    # Blanket string swap covers sprite + glyphs + anything else that names the
    # upstream host. The vector source URL is corrected specifically below.
    serialised = json.dumps(style).replace(UPSTREAM_ORIGIN, origin)
    rewritten: dict[str, Any] = json.loads(serialised)

    vector_sources = [
        name
        for name, spec in rewritten.get("sources", {}).items()
        if spec.get("type") == "vector"
    ]
    if not vector_sources:
        raise ValueError(
            "no vector source found in style — upstream shape changed, "
            "review before trusting the rewrite"
        )

    template = f"{origin}/{tile_path.lstrip('/')}"
    for name in vector_sources:
        spec = rewritten["sources"][name]
        # Drop the upstream TileJSON pointer: `url` and `tiles` together is
        # ambiguous, and MapLibre would fetch the former to discover the latter.
        spec.pop("url", None)
        spec["tiles"] = [template]
        # Replaces what the dropped TileJSON used to supply — see the docstring.
        spec["minzoom"] = min_zoom
        spec["maxzoom"] = max_zoom

    return rewritten


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — parse args, rewrite, and emit the style to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=UPSTREAM_STYLE_URL,
        help=f"style URL or local path (default: {UPSTREAM_STYLE_URL})",
    )
    parser.add_argument(
        "--origin",
        required=True,
        help="self-hosted origin, e.g. https://tiles.snowdesk-data.info",
    )
    parser.add_argument(
        "--tile-path",
        default="tiles/{z}/{x}/{y}.mvt",
        help=(
            "XYZ template the Worker serves, relative to the origin "
            "(default: tiles/{z}/{x}/{y}.mvt)"
        ),
    )
    parser.add_argument(
        "--min-zoom",
        type=int,
        default=DEFAULT_MIN_ZOOM,
        help=f"shallowest zoom in the archive (default: {DEFAULT_MIN_ZOOM})",
    )
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=DEFAULT_MAX_ZOOM,
        help=f"deepest zoom in the archive (default: {DEFAULT_MAX_ZOOM})",
    )
    args = parser.parse_args(argv)

    style = load_style(args.source)
    rewritten = rewrite(
        style, args.origin, args.tile_path, args.min_zoom, args.max_zoom
    )
    json.dump(rewritten, sys.stdout, indent=2)
    sys.stdout.write("\n")

    residual = json.dumps(rewritten).count(UPSTREAM_ORIGIN)
    if residual:
        sys.stderr.write(
            f"WARNING: {residual} residual {UPSTREAM_ORIGIN} reference(s) remain\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
