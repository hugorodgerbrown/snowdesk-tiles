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

# Credit for the vector data, shown in the map UI. Required, not decorative: the
# OpenMapTiles schema asks for it and OpenStreetMap's ODbL obliges it, and the
# Snowdesk legend's "Map data" section is built by reading ``attribution`` off
# each runtime source object.
#
# It has to be stated here for the same reason the zoom range does — upstream
# keeps it in the TileJSON that the vector source's ``url`` points at, so
# dropping ``url`` for a ``tiles`` template drops the attribution with it. The
# legend then renders as a bare heading with nothing under it, which is a
# licensing gap and not just a visual one (SNOW-640).
#
# Deliberately the same string the Worker puts in its own TileJSON
# (``worker/src/index.js``); ``verify.sh`` checks the published style against
# the live TileJSON so the two cannot drift apart.
#
# Upstream's OpenFreeMap credit is not carried over. They no longer serve any of
# this data — planetiler builds the archive from OSM in ``build-extract.sh`` —
# so naming them as its source would be wrong. Their style, sprites and glyphs
# are BSD-licensed and need no UI credit.
DEFAULT_ATTRIBUTION = (
    '<a href="https://openmaptiles.org/">&copy; OpenMapTiles</a> '
    '<a href="https://www.openstreetmap.org/copyright">'
    "&copy; OpenStreetMap contributors</a>"
)

# Liberty layers a Natural Earth shaded-relief raster under the vector data at
# low zoom, mirrored into the bucket by ``mirror_assets.py``. Public domain, so
# a name check is the customary credit rather than an obligation — but a source
# serving our origin with no attribution at all is what SNOW-640 was about.
RASTER_ATTRIBUTION = '<a href="https://www.naturalearthdata.com/">Natural Earth</a>'


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
    attribution: str = DEFAULT_ATTRIBUTION,
) -> dict[str, Any]:
    """Repoint every upstream URL at ``origin`` and tiles at ``tile_path``.

    Every ``tiles.openfreemap.org`` occurrence (sprite, glyphs, and any stray
    reference) is swapped for ``origin``; each vector source is rewritten to an
    XYZ ``tiles`` template served by the Worker, carrying the archive's own
    ``[min_zoom, max_zoom]`` range and ``attribution``.

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

    The attribution is mandatory for the same reason and lost the same way: the
    dropped TileJSON carried it, and the Snowdesk legend builds its "Map data"
    section from the ``attribution`` on each runtime source. With none on any
    source the section renders as a bare heading and the required OpenStreetMap
    and OpenMapTiles credits are simply absent (SNOW-640). Every source is left
    carrying one — a source that already declares its own keeps it.

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
        # Set rather than defaulted: this archive is built here from OSM, so its
        # credit is ours to state whatever upstream said about theirs.
        spec["attribution"] = attribution

    # Anything else we serve — the Natural Earth raster — gets a credit too, but
    # only where the style does not already carry one of its own.
    for spec in rewritten.get("sources", {}).values():
        if spec.get("type") == "raster" and not spec.get("attribution"):
            spec["attribution"] = RASTER_ATTRIBUTION

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
    parser.add_argument(
        "--attribution",
        default=DEFAULT_ATTRIBUTION,
        help="credit written onto the vector source (default: OpenMapTiles + OSM)",
    )
    args = parser.parse_args(argv)

    style = load_style(args.source)
    rewritten = rewrite(
        style,
        args.origin,
        args.tile_path,
        args.min_zoom,
        args.max_zoom,
        args.attribution,
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
