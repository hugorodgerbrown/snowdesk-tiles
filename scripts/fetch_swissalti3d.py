#!/usr/bin/env python3
r"""List the swissALTI3D tiles covering a bounding box, ready to download.

SNOW-908. swisstopo publish swissALTI3D through a STAC API at
``data.geo.admin.ch``: one item per square kilometre, each carrying the same
patch of ground as a 0.5 m and a 2 m GeoTIFF. This script walks that API for a
bounding box and writes two files — a plain list of asset URLs for the download
step, and a manifest recording what was selected, which is what the build reads
its target extent and its provenance out of.

It does not download anything. Fetching ~42,000 GeoTIFFs is a job for ``xargs``
and ``curl`` in ``build-terrain.sh``, which can resume, parallelise and be
interrupted; this half is the part with decisions in it, so it is the part worth
testing.

Two decisions, neither obvious from the API:

* **The 2 m asset, not the 0.5 m one.** The target grid is 5 m, so 0.5 m is
  sixteen times the download and the throughput for detail that is averaged away
  in the same pass. 2 m still oversamples 5 m by enough that the box filter has
  something to average.
* **One item per square kilometre, newest wins.** swissALTI3D is re-surveyed on
  a six-year cycle and the item id carries the survey year, so a re-flown square
  can appear more than once. Every item found today is 2019, but a build run
  after the next revision would silently mix two vintages at a seam, which is
  exactly the kind of thing nobody would think to look for.

Standalone operational tooling: stdlib only, no Django.

Usage:
    python scripts/fetch_swissalti3d.py \
        --bbox 5.95 45.72 10.50 47.83 \
        --urls work/urls.txt --manifest work/manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STAC_ORIGIN = os.environ.get("STAC_ORIGIN", "https://data.geo.admin.ch")
COLLECTION = os.environ.get("SWISSALTI3D_COLLECTION", "ch.swisstopo.swissalti3d")
COLLECTION_URL = f"{STAC_ORIGIN}/api/stac/v1/collections/{COLLECTION}"

#: Items per page. The API caps this; 100 is what it serves.
PAGE_SIZE = 100

#: Guard against a paging loop. Switzerland is ~42,000 items, so this is two
#: orders of magnitude of headroom rather than a real limit.
MAX_PAGES = 2000


def fetch_json(url: str) -> dict[str, Any]:
    """Return the JSON document at ``url``."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - checked
        document: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return document


def items(bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Return every STAC item intersecting a WGS84 bounding box."""
    query = urllib.parse.urlencode(
        {"bbox": ",".join(str(value) for value in bbox), "limit": PAGE_SIZE}
    )
    url: str | None = f"{COLLECTION_URL}/items?{query}"
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    for _ in range(MAX_PAGES):
        if url is None:
            return found
        if url in seen:
            raise RuntimeError(f"STAC paging looped back to {url}")
        seen.add(url)
        page = fetch_json(url)
        found.extend(page.get("features", []))
        url = next(
            (
                link["href"]
                for link in page.get("links", [])
                if link.get("rel") == "next"
            ),
            None,
        )
        print(f"    {len(found)} items", end="\r", file=sys.stderr)

    raise RuntimeError(f"STAC paging exceeded {MAX_PAGES} pages")


def square_of(item: dict[str, Any]) -> str:
    """Return the kilometre-square key from an item id."""
    # swissalti3d_2019_2608-1094 -> "2608-1094"
    parts = str(item["id"]).split("_")
    if len(parts) != 3:
        raise ValueError(f"unexpected swissALTI3D item id: {item['id']!r}")
    return parts[2]


def survey_year(item: dict[str, Any]) -> int:
    """Return the survey year an item was flown in."""
    return int(item["id"].split("_")[1])


def asset_href(item: dict[str, Any], gsd: float) -> str | None:
    """Return the GeoTIFF asset at ``gsd`` metres, or None if there is none."""
    for name, asset in sorted(item.get("assets", {}).items()):
        if not name.endswith(".tif"):
            continue
        if float(asset.get("gsd", 0)) == gsd:
            href: str = asset["href"]
            return href
    return None


def select(found: list[dict[str, Any]], gsd: float) -> list[dict[str, Any]]:
    """Return one item per kilometre square, keeping the latest survey."""
    newest: dict[str, dict[str, Any]] = {}
    for item in found:
        square = square_of(item)
        previous = newest.get(square)
        if previous is None or survey_year(item) > survey_year(previous):
            newest[square] = item
    chosen = []
    for square in sorted(newest):
        item = newest[square]
        href = asset_href(item, gsd)
        if href is None:
            print(
                f"warning: {item['id']} has no {gsd} m GeoTIFF — skipped",
                file=sys.stderr,
            )
            continue
        chosen.append(item | {"_href": href})
    return chosen


def manifest(chosen: list[dict[str, Any]], gsd: float) -> dict[str, Any]:
    """Summarise a selection: its extent, its vintage and where it came from.

    The bounding box here is the union of what was actually selected, not the
    box that was asked for. That distinction is what makes it safe to feed
    straight into the grid extent: coverage stops at the national border, and
    snapping the requested box would warp a wide margin of nothing.
    """
    boxes = [item["bbox"] for item in chosen]
    return {
        "collection": COLLECTION,
        "gsd": gsd,
        "count": len(chosen),
        "bbox": [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
        "survey_years": sorted({survey_year(item) for item in chosen}),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — walk the STAC API and write the URL list and manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("W", "S", "E", "N"),
        help="WGS84 bounding box to cover",
    )
    parser.add_argument(
        "--gsd",
        type=float,
        default=2.0,
        help="source resolution in metres (default: 2.0)",
    )
    parser.add_argument("--urls", required=True, help="write the asset URL list here")
    parser.add_argument("--manifest", required=True, help="write the manifest here")
    args = parser.parse_args(argv)

    print(f"==> querying {COLLECTION} over {args.bbox}", file=sys.stderr)
    found = items(tuple(args.bbox))
    if not found:
        print("error: no swissALTI3D items in that box", file=sys.stderr)
        return 1

    chosen = select(found, args.gsd)
    if not chosen:
        print(f"error: no {args.gsd} m assets on {len(found)} items", file=sys.stderr)
        return 1

    Path(args.urls).write_text(
        "".join(f"{item['_href']}\n" for item in chosen), encoding="utf-8"
    )
    summary = manifest(chosen, args.gsd)
    Path(args.manifest).write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    dropped = len(found) - len(chosen)
    print(
        f"==> {summary['count']} squares, survey years {summary['survey_years']}"
        + (f" ({dropped} superseded or unusable)" if dropped else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
