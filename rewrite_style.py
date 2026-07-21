#!/usr/bin/env python3
"""Rewrite the OpenFreeMap Liberty style JSON for the self-hosted origin.

SNOW-485. The public Liberty style at ``tiles.openfreemap.org`` references its
own host in three places — the vector ``sources`` URL, the ``sprite`` base, and
the ``glyphs`` template. Self-hosting means serving a copy of that style whose
internal URLs point back at ``tiles.snowdesk.info`` instead, and whose vector
source reads the local PMTiles archive via the client-side ``pmtiles://``
protocol rather than the OpenFreeMap tile server.

Standalone operational tooling: stdlib only, no Django. Run it when building or
refreshing the disk contents; write the output to ``/data/styles/liberty``.

Usage (ORIGIN = https://tiles.snowdesk.info):
    python rewrite_style.py --origin ORIGIN --pmtiles alps.pmtiles > liberty

    # Rewrite a local copy instead of fetching the upstream style:
    python rewrite_style.py --source ./liberty.json --origin ORIGIN --pmtiles ...
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

UPSTREAM_STYLE_URL = "https://tiles.openfreemap.org/styles/liberty"
UPSTREAM_ORIGIN = "https://tiles.openfreemap.org"


def load_style(source: str) -> dict[str, Any]:
    """Return the style JSON, fetched over HTTP or read from a local path."""
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as response:  # noqa: S310 - trusted URL
            return json.loads(response.read().decode("utf-8"))
    with open(source, encoding="utf-8") as handle:
        return json.load(handle)


def rewrite(style: dict[str, Any], origin: str, pmtiles: str) -> dict[str, Any]:
    """Repoint every upstream URL at ``origin`` and read tiles from ``pmtiles``.

    Every ``tiles.openfreemap.org`` occurrence (sprite, glyphs, and any stray
    reference) is swapped for ``origin``; the first vector source is rewritten
    to a ``pmtiles://`` URL so MapLibre reads the local archive directly via
    range requests. Raises if no vector source is found — that would mean the
    upstream style shape changed and the rewrite is no longer safe to trust.
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

    for name in vector_sources:
        spec = rewritten["sources"][name]
        # Drop any server-generated tile endpoints; the client-side reader only
        # needs the pmtiles:// url.
        spec.pop("tiles", None)
        spec["url"] = f"pmtiles://{origin}/{pmtiles}"

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
        help="self-hosted origin, e.g. https://tiles.snowdesk.info",
    )
    parser.add_argument(
        "--pmtiles",
        required=True,
        help="PMTiles filename on the origin, e.g. alps.pmtiles",
    )
    args = parser.parse_args(argv)

    style = load_style(args.source)
    rewritten = rewrite(style, args.origin, args.pmtiles)
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
