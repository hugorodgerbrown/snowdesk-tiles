"""Choose and record the Mapterhorn archives the elevation copy is cut from.

SNOW-1444. The 3-D map's elevation copy is Mapterhorn's Terrarium tiles clipped
to Snowdesk's supported area, the GeoJSON that snowdesk-data-pipeline's
``export_supported_area`` writes (committed here as dem/supported-area.geojson).
Mapterhorn publishes z0-12 as ``planet.pmtiles`` and z13+ as one archive per z6
tile, ``6-<x>-<y>.pmtiles``, listed with their bounds in ``download_urls.json``.
This picks the regional archives the area touches, so build-dem.sh extracts from
those and no others, and writes the manifest published beside the copy.

Stdlib only, like the rest of the build tooling::

    python3 scripts/dem_archives.py select AREA CATALOGUE
    python3 scripts/dem_archives.py manifest VERSION AREA CATALOGUE NAME...
    python3 scripts/dem_archives.py locate LON LAT Z
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLANET = "planet.pmtiles"
# The zoom Mapterhorn's regional archives start at. Below it, planet.pmtiles.
REGIONAL_MIN_ZOOM = 13

Box = tuple[float, float, float, float]


def _polygons(geometry: dict[str, Any]) -> Iterator[list[list[list[float]]]]:
    """Yield each polygon's rings from a Polygon or MultiPolygon geometry."""
    kind = geometry["type"]
    if kind == "Polygon":
        yield geometry["coordinates"]
    elif kind == "MultiPolygon":
        yield from geometry["coordinates"]
    else:
        raise ValueError(f"unsupported geometry type {kind!r}")


def area_boxes(area: dict[str, Any]) -> list[Box]:
    """Return one (west, south, east, north) box per polygon in the area.

    Accepts a FeatureCollection, a Feature or a bare geometry. A box per
    polygon, rather than one round everything, keeps a regional archive
    between two parts of the area (the Pyrenees and the Alps, say) out.
    """
    if area.get("type") == "FeatureCollection":
        geometries = [feature["geometry"] for feature in area["features"]]
    elif area.get("type") == "Feature":
        geometries = [area["geometry"]]
    else:
        geometries = [area]

    boxes = []
    for geometry in geometries:
        for polygon in _polygons(geometry):
            outer = polygon[0]
            lons = [point[0] for point in outer]
            lats = [point[1] for point in outer]
            boxes.append((min(lons), min(lats), max(lons), max(lats)))
    if not boxes:
        raise ValueError("the area has no polygons")
    return boxes


def _overlaps(a: Box, b: Box) -> bool:
    """Whether two boxes share some area; touching along an edge does not count.

    The supported area is built from whole z12 tiles, which never straddle a z6
    tile's edge, so an archive whose box only touches the area holds none of it.
    """
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def select(area: dict[str, Any], catalogue: dict[str, Any]) -> list[str]:
    """Return the regional archives (z13+) whose bounds overlap the area.

    Sorted by name, so the build order and the manifest are stable.
    """
    boxes = area_boxes(area)
    chosen = []
    for item in catalogue["items"]:
        if item["name"] == PLANET or item["min_zoom"] < REGIONAL_MIN_ZOOM:
            continue
        bounds = (item["min_lon"], item["min_lat"], item["max_lon"], item["max_lat"])
        if any(_overlaps(bounds, box) for box in boxes):
            chosen.append(item["name"])
    return sorted(chosen)


def manifest(
    version: str,
    area_bytes: bytes,
    catalogue: dict[str, Any],
    names: list[str],
    max_zoom: int,
) -> dict[str, Any]:
    """Describe one published copy: its sources, its area and when it was cut.

    Published as dem/<version>/manifest.json, so a copy in the bucket can be
    traced to the Mapterhorn release and the area it was cut from.
    """
    by_name = {item["name"]: item for item in catalogue["items"]}
    missing = sorted(set(names) - set(by_name))
    if missing:
        raise ValueError(f"not in the catalogue: {', '.join(missing)}")
    return {
        "version": version,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Mapterhorn",
        "source_release": catalogue.get("version"),
        "max_zoom": max_zoom,
        "area_sha256": hashlib.sha256(area_bytes).hexdigest(),
        "archives": [
            {"name": name, "source_md5": by_name[name]["md5sum"]}
            for name in [PLANET, *names]
        ],
    }


def locate(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Return the Web Mercator (x, y) tile holding a point at zoom z."""
    n = 2**z
    x = math.floor((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = math.floor((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return x, y


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    picker = commands.add_parser("select", help="regional archives to extract")
    picker.add_argument("area", type=Path)
    picker.add_argument("catalogue", type=Path)

    writer = commands.add_parser("manifest", help="print the copy's manifest")
    writer.add_argument("version")
    writer.add_argument("area", type=Path)
    writer.add_argument("catalogue", type=Path)
    writer.add_argument("names", nargs="*")
    writer.add_argument("--max-zoom", type=int, required=True)

    locator = commands.add_parser("locate", help="tile x y for a point")
    locator.add_argument("lon", type=float)
    locator.add_argument("lat", type=float)
    locator.add_argument("z", type=int)

    args = parser.parse_args(argv)
    if args.command == "select":
        area = json.loads(args.area.read_text())
        catalogue = json.loads(args.catalogue.read_text())
        for name in select(area, catalogue):
            print(name)
    elif args.command == "manifest":
        catalogue = json.loads(args.catalogue.read_text())
        document = manifest(
            args.version,
            args.area.read_bytes(),
            catalogue,
            args.names,
            args.max_zoom,
        )
        print(json.dumps(document, indent=2))
    else:
        x, y = locate(args.lon, args.lat, args.z)
        print(args.z, x, y)
    return 0


if __name__ == "__main__":
    sys.exit(main())
