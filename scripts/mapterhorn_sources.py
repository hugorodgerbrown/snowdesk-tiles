#!/usr/bin/env python3
r"""Choose which Mapterhorn sources the terrain grid takes, and credit them.

SNOW-1505. Mapterhorn publishes one merged surface: a national model wherever
an open one exists, Copernicus GLO-30 everywhere else, with no per-pixel marker
of which is which. The terrain grid must not take the GLO-30 part — a 30 m
radar height resampled onto a 5 m grid and labelled lidar is the failure
``native_resolution_m`` and the 204 rule exist to prevent — so the build clips
the Mapterhorn fill to the coverage polygons of the national models alone, and
GLO-30-only ground stays absent (204).

This module makes the choices that clipping turns on. Stdlib plus numpy:

* ``probe`` prints what ``download.mapterhorn.com`` lists beside
  ``attribution.json``, so an operator can find the coverage file and confirm
  the attribution's shape before a build relies on it.
* ``select`` reads the per-source coverage polygons (GeoJSON; build-terrain.sh
  converts a GeoPackage with ogr2ogr first), drops GLO-30, keeps the sources
  whose polygon meets the area, and writes the clip file gdalwarp cuts with and
  the credits grid.json publishes. Polygons are used as published, with no
  inward buffer.
* ``manifest`` widens the swissALTI3D fetch manifest to the Mapterhorn area and
  records the Mapterhorn release, so the grid extent covers both and
  ``grid.json`` can say where each source's heights came from.
* ``verify-gap`` writes the coverage-derived expectations verify.sh checks: a
  point inside the supported area but outside every national model (expects
  204), and which stretch of each seam profile a national model covers.
* ``dem-zoom`` stops a build reading an elevation copy cut shallower than z14.

Usage:
    python scripts/mapterhorn_sources.py probe
    python scripts/mapterhorn_sources.py select --coverage cov.geojson \
        --attribution attribution.json --area dem/supported-area.geojson \
        --bbox 5.0 43.7 16.6 48.4 --clip clip.geojson --credits credits.json
    python scripts/mapterhorn_sources.py manifest --swiss swiss.json \
        --area dem/supported-area.geojson --bbox 5.0 43.7 16.6 48.4 \
        --credits credits.json --dem-manifest dem.json --dem-version 2026-12
    python scripts/mapterhorn_sources.py dem-zoom dem.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from terrain_grid import HEIGHT_NODATA_M
from terrarium import (
    ZOOM,
    Box,
    Tile,
    geometries,
    rings,
    scanline_cells,
    tile_lonlat_bounds,
    tiles_bbox,
    tiles_for_area,
    to_tile_space,
)

#: Where Mapterhorn's catalogue and attribution live (DEM_SOURCE_ORIGIN).
DEFAULT_ORIGIN = "https://download.mapterhorn.com"

#: The zoom the area is tested at when deciding which polygons meet it. The
#: supported area is a union of z12 tiles, so at z12 "the area" is an exact set
#: of cells rather than an approximation of one.
AREA_ZOOM = 12

#: The deepest zoom the terrain build reads from the elevation copy.
REQUIRED_DEM_ZOOM = ZOOM

#: Feature properties a coverage polygon's source id may be under, in order.
#: The coverage file's schema is Mapterhorn's, not ours; ``probe`` shows it.
SOURCE_FIELDS = ("source", "source_id", "id", "name")

#: How far either side of the seam the profile's coverage test leaves alone,
#: in metres: the seam is located to a sample, so the sample either side of it
#: may belong to either source.
SEAM_SLACK_M = 5.0


# --- Sources ----------------------------------------------------------------


def is_glo30(source_id: str) -> bool:
    """Whether a source id is Copernicus GLO-30, under any catalogue prefix.

    The catalogue also lists ``debug-glo30``; that is GLO-30 too.
    """
    return source_id == "glo30" or source_id.endswith("-glo30")


def source_id(feature: dict[str, Any], fields: Sequence[str] = SOURCE_FIELDS) -> str:
    """Return the source a coverage feature belongs to."""
    properties = feature.get("properties") or {}
    for field in fields:
        value = properties.get(field)
        if value not in (None, ""):
            return str(value)
    raise ValueError(
        f"coverage feature has none of {', '.join(fields)} in its properties "
        f"({sorted(properties)}); pass --source-field"
    )


def parse_resolution(value: Any) -> float:
    """Return a resolution in metres from a number or a string such as ``"1 m"``."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    match = re.match(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:m|metres?|meters?)?\s*$", str(value)
    )
    if not match:
        raise ValueError(f"cannot read a resolution in metres from {value!r}")
    return float(match.group(1))


def attribution_entries(document: Any) -> dict[str, dict[str, Any]]:
    """Return attribution.json's entries keyed by source id.

    Mapterhorn's catalogue folders carry ``{name, website, license, producer,
    resolution}``; attribution.json is read as a list of such objects carrying
    their id, a ``{"sources": [...]}`` wrapper round one, or an object keyed by
    id. ``probe`` prints the live file so a change of shape is seen before a
    build depends on it.
    """
    if isinstance(document, dict) and isinstance(document.get("sources"), list):
        document = document["sources"]
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(document, list):
        for item in document:
            key = next(
                (str(item[f]) for f in ("source", "id", "source_id") if item.get(f)),
                None,
            )
            if key is None:
                raise ValueError(f"attribution entry without an id: {item!r}")
            entries[key] = item
    elif isinstance(document, dict):
        entries = {str(key): dict(value) for key, value in document.items()}
    else:
        raise ValueError("attribution.json is neither a list nor an object")
    return entries


def credit(source: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Return one ``credits[]`` item of grid.json's mapterhorn entry."""
    producer = entry.get("producer") or entry.get("name") or source
    licence = entry.get("license") or entry.get("licence")
    if not licence:
        raise ValueError(f"attribution for {source} names no licence")
    resolution = entry.get("resolution", entry.get("native_resolution_m"))
    if resolution is None:
        raise ValueError(f"attribution for {source} states no resolution")
    return {
        "id": source,
        "name": entry.get("name") or source,
        "producer": producer,
        "licence": licence,
        "url": entry.get("website") or entry.get("url") or entry.get("license_url"),
        "native_resolution_m": parse_resolution(resolution),
        "attribution": entry.get("attribution") or f"© {producer}",
    }


# --- Geometry ---------------------------------------------------------------


def _hits_cell(x0: float, y0: float, x1: float, y1: float, cell: Tile) -> bool:
    """Whether a segment in tile space passes through a cell's interior.

    Liang-Barsky against the closed cell, then the clipped piece's midpoint is
    tested against the open one, so a segment running along a cell's edge does
    not count — the supported area's own edges lie on cell edges.
    """
    left, top = cell
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in (
        (-dx, x0 - left),
        (dx, left + 1 - x0),
        (-dy, y0 - top),
        (dy, top + 1 - y0),
    ):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    mid = (t0 + t1) / 2
    mx, my = x0 + mid * dx, y0 + mid * dy
    return left < mx < left + 1 and top < my < top + 1


def cells_met(geometry: dict[str, Any], cells: set[Tile], zoom: int) -> set[Tile]:
    """Return the cells of ``cells`` a polygon's interior reaches into.

    A cell is met when the polygon holds its centre, or holds a vertex inside
    it, or has an edge crossing it — between them, every way two areas can
    overlap without one being wholly inside a single cell of the other.
    """
    if not cells:
        return set()
    tile_rings = to_tile_space(rings(geometry), zoom)
    met = scanline_cells(tile_rings) & cells
    cell_list = sorted(cells)
    for ring in tile_rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
            vertex = (math.floor(x0), math.floor(y0))
            if vertex in cells:
                met.add(vertex)
            cols = range(math.floor(min(x0, x1)), math.floor(max(x0, x1)) + 1)
            rows = range(math.floor(min(y0, y1)), math.floor(max(y0, y1)) + 1)
            if len(cols) * len(rows) <= len(cells):
                candidates: Iterable[Tile] = (
                    (c, r) for c in cols for r in rows if (c, r) in cells
                )
            else:
                candidates = (c for c in cell_list if c[0] in cols and c[1] in rows)
            for cell in candidates:
                if cell not in met and _hits_cell(x0, y0, x1, y1, cell):
                    met.add(cell)
    return met


def area_cells(area: dict[str, Any], bbox: Box) -> set[Tile]:
    """Return the area's z12 cells that overlap the box."""
    return set(tiles_for_area(area, AREA_ZOOM, bbox))


def contains(
    geometry: dict[str, Any], points: Sequence[tuple[float, float]]
) -> list[bool]:
    """Return, for each (lon, lat), whether a polygon geometry holds it.

    Even-odd over every ring at once, vectorised over the edges: a national
    coverage polygon has too many edges for a Python loop per point.
    """
    starts, ends = [], []
    for ring in rings(geometry):
        coords = np.asarray(ring, dtype=np.float64)
        if len(coords) < 3:
            continue
        starts.append(coords)
        ends.append(np.roll(coords, -1, axis=0))
    if not starts:
        return [False] * len(points)
    a = np.concatenate(starts)
    b = np.concatenate(ends)
    result = []
    for lon, lat in points:
        straddles = (a[:, 1] > lat) != (b[:, 1] > lat)
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing_x = a[:, 0] + (lat - a[:, 1]) * (b[:, 0] - a[:, 0]) / (
                b[:, 1] - a[:, 1]
            )
        result.append(bool(np.count_nonzero(straddles & (lon < crossing_x)) % 2))
    return result


# --- select -----------------------------------------------------------------


def select(
    coverage: dict[str, Any],
    attribution: Any,
    area: dict[str, Any],
    bbox: Box,
    fields: Sequence[str] = SOURCE_FIELDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the clip FeatureCollection and the credits for the Mapterhorn fill.

    The clip holds every non-GLO-30 coverage polygon that meets the area,
    unchanged; gdalwarp unions them as its cutline. Credits are one per source
    kept, sorted by id, each from attribution.json. ``native_resolution_m`` is
    the coarsest kept model's — the honest figure for a mosaic of them.
    """
    cells = area_cells(area, bbox)
    if not cells:
        raise ValueError("the supported area and the box share no ground")
    entries = attribution_entries(attribution)

    kept: list[dict[str, Any]] = []
    sources: set[str] = set()
    for feature in coverage.get("features", []):
        if not feature.get("geometry"):
            continue
        source = source_id(feature, fields)
        if is_glo30(source):
            continue
        if not cells_met(feature["geometry"], cells, AREA_ZOOM):
            continue
        kept.append(
            {
                "type": "Feature",
                "properties": {"source": source},
                "geometry": feature["geometry"],
            }
        )
        sources.add(source)

    if not kept:
        raise ValueError("no national model's coverage meets the area")
    missing = sorted(sources - set(entries))
    if missing:
        raise ValueError(
            f"attribution.json has no entry for {', '.join(missing)} — its "
            "heights cannot be published without its credit"
        )
    credits = [credit(source, entries[source]) for source in sorted(sources)]
    clip = {"type": "FeatureCollection", "features": kept}
    return clip, {
        "credits": credits,
        "native_resolution_m": max(c["native_resolution_m"] for c in credits),
    }


# --- manifest ---------------------------------------------------------------


def mapterhorn_area_bbox(area: dict[str, Any], bbox: Box) -> Box:
    """Return the WGS84 bbox of the z14 tiles the build fetches."""
    tiles = tiles_for_area(area, ZOOM, bbox)
    if not tiles:
        raise ValueError("the supported area and the box share no ground")
    return tiles_bbox(tiles, ZOOM)


def union_bbox(a: Sequence[float], b: Sequence[float]) -> list[float]:
    """Return the smallest box holding both."""
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def merge_manifest(
    swiss: dict[str, Any],
    area_bbox: Sequence[float],
    credits: dict[str, Any],
    dem_manifest: dict[str, Any],
    dem_version: str,
) -> dict[str, Any]:
    """Return the fetch manifest widened to cover the Mapterhorn area too.

    The top-level ``bbox`` is what ``terrain_grid.py extent`` snaps the warp
    extent from, so widening it is what puts the rest of the Alps on the grid.
    The swissALTI3D figures move under ``sources`` unchanged: its coverage is
    published from its own extent, never from the merged raster.
    """
    merged = dict(swiss)
    merged["bbox"] = union_bbox(swiss["bbox"], area_bbox)
    merged["sources"] = {
        "swissalti3d": {
            "bbox": list(swiss["bbox"]),
            "count": swiss.get("count"),
            "survey_years": swiss.get("survey_years", []),
        },
        "mapterhorn": {
            "bbox": list(area_bbox),
            "release": dem_manifest.get("source_release"),
            "dem_version": dem_version,
            "native_resolution_m": credits["native_resolution_m"],
            "credits": credits["credits"],
        },
    }
    return merged


def check_dem_zoom(dem_manifest: dict[str, Any]) -> str | None:
    """Return why the elevation copy cannot feed the build, or None if it can."""
    max_zoom = dem_manifest.get("max_zoom")
    if not isinstance(max_zoom, int) or max_zoom < REQUIRED_DEM_ZOOM:
        return (
            f"the elevation copy records max_zoom {max_zoom!r}; the terrain build "
            f"reads z{REQUIRED_DEM_ZOOM}, so the copy must hold it"
        )
    return None


# --- verify-gap -------------------------------------------------------------


def find_gap(
    clip: dict[str, Any],
    area: dict[str, Any],
    bbox: Box,
    swiss_bbox: Sequence[float],
) -> dict[str, Any] | None:
    """Return a point in the supported area that no national model covers.

    The centre of a z12 cell inside the box that no clip polygon meets and
    swissALTI3D's extent does not touch, preferring one whose neighbours are
    unmet too. A z12 cell is several kilometres across and a grid tile 1.28 km,
    so the tile holding its centre is wholly uncovered and answers 204. None
    when every cell is covered — there is then no gap to probe.
    """
    cells = {
        cell
        for cell in area_cells(area, bbox)
        if _inside(tile_lonlat_bounds(*cell, AREA_ZOOM), bbox)
    }
    met: set[Tile] = set()
    for feature in clip.get("features", []):
        met |= cells_met(feature["geometry"], cells, AREA_ZOOM)
    swiss = (swiss_bbox[0], swiss_bbox[1], swiss_bbox[2], swiss_bbox[3])
    free = sorted(
        cell
        for cell in cells - met
        if not _touches(tile_lonlat_bounds(*cell, AREA_ZOOM), swiss)
    )
    if not free:
        return None
    free_set = set(free)

    def isolated(cell: Tile) -> bool:
        x, y = cell
        return all(
            (x + dx, y + dy) in free_set for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        )

    chosen = next((cell for cell in free if isolated(cell)), free[0])
    west, south, east, north = tile_lonlat_bounds(*chosen, AREA_ZOOM)
    return {
        "name": f"gap z{AREA_ZOOM}/{chosen[0]}/{chosen[1]}",
        "lon": round((west + east) / 2, 6),
        "lat": round((south + north) / 2, 6),
    }


def _inside(inner: Box, outer: Box) -> bool:
    """Whether one box lies within another."""
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _touches(a: Box, b: Box) -> bool:
    """Whether two boxes share any point, edges included."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def intervals(distances: Sequence[float], flags: Sequence[bool]) -> list[list[float]]:
    """Return the runs of consecutive true flags as [first, last] distances."""
    runs: list[list[float]] = []
    previous = False
    for distance, flag in zip(distances, flags, strict=True):
        if flag and previous:
            runs[-1][1] = distance
        elif flag:
            runs.append([distance, distance])
        previous = flag
    return runs


def seam_from_swiss(distances: Sequence[float], swiss_valid: Sequence[bool]) -> float:
    """Return where along a profile swissALTI3D stops: the profile's seam.

    The midpoint between the last Swiss sample and the first sample past it,
    taking the transition nearest the profile's centre. Distances run from the
    Swiss side (negative) to the foreign side (positive).
    """
    transitions = [
        (distances[i] + distances[i + 1]) / 2
        for i in range(len(distances) - 1)
        if swiss_valid[i] and not swiss_valid[i + 1]
    ]
    if not transitions:
        raise ValueError(
            "swissALTI3D does not stop anywhere along the profile — check its "
            "centre is on the border and its bearing points out of Switzerland"
        )
    return min(transitions, key=abs)


def profile_expectations(
    clip: dict[str, Any],
    area: dict[str, Any],
    samples: Sequence[tuple[float, float, float]],
    swiss_valid: Sequence[bool],
) -> dict[str, Any]:
    """Return where along one profile verify.sh should find heights.

    ``samples`` are (distance, lon, lat). ``covered`` lists the stretches a
    national model and the supported area both hold; ``foreign_covered`` says
    whether any of the foreign side is among them. Where it is not, verify.sh
    expects no height on that side at all.
    """
    distances = [sample[0] for sample in samples]
    points = [(sample[1], sample[2]) for sample in samples]
    seam = seam_from_swiss(distances, swiss_valid)

    in_area = [any(flags) for flags in zip(*_each(area, points), strict=True)]
    in_clip = [False] * len(points)
    for feature in clip.get("features", []):
        for index, inside in enumerate(contains(feature["geometry"], points)):
            in_clip[index] = in_clip[index] or inside
    covered = [a and c for a, c in zip(in_area, in_clip, strict=True)]
    foreign = [
        flag
        for distance, flag in zip(distances, covered, strict=True)
        if distance > seam + SEAM_SLACK_M
    ]
    return {
        "seam_m": seam,
        "covered": intervals(distances, covered),
        "foreign_covered": any(foreign),
    }


def _each(
    document: dict[str, Any], points: Sequence[tuple[float, float]]
) -> list[list[bool]]:
    """Return, per geometry in a document, which points it holds."""
    return [contains(geometry, points) for geometry in geometries(document)]


# --- probe ------------------------------------------------------------------


def fetch_bytes(url: str) -> bytes:
    """Return the body at an http(s) URL."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - checked
        body: bytes = response.read()
    return body


def probe(origin: str, fetch: Callable[[str], bytes] = fetch_bytes) -> list[str]:
    """Return the lines ``probe`` prints about what the origin publishes."""
    origin = origin.rstrip("/")
    catalogue = json.loads(fetch(f"{origin}/download_urls.json"))
    lines = [f"release {catalogue.get('version')}"]
    items = catalogue.get("items", [])
    archives = [i for i in items if str(i.get("name", "")).endswith(".pmtiles")]
    others = [i for i in items if i not in archives]
    lines.append(f"{len(archives)} pmtiles archives")
    lines += [f"other: {i.get('name')} {i.get('url', '')}".rstrip() for i in others]
    lines += [
        f"key: {key}" for key in sorted(catalogue) if key not in {"items", "version"}
    ]
    try:
        attribution = json.loads(fetch(f"{origin}/attribution.json"))
    except (OSError, ValueError) as error:
        lines.append(f"attribution.json: unreadable ({error})")
        return lines
    entries = attribution_entries(attribution)
    lines.append(f"attribution.json: {len(entries)} sources")
    for key in sorted(entries):
        entry = entries[key]
        licence = entry.get("license") or entry.get("licence")
        lines.append(
            f"  {key}: {entry.get('name')} / {licence} / {entry.get('resolution')}"
        )
    return lines


# --- CLI --------------------------------------------------------------------


def _read(path: Path) -> Any:
    """Return the JSON document at ``path``."""
    return json.loads(path.read_text(encoding="utf-8"))


def _bbox(values: Sequence[float]) -> Box:
    """Return four floats as a Box."""
    return (values[0], values[1], values[2], values[3])


def _add_bbox(parser: argparse.ArgumentParser) -> None:
    """Add the required ``--bbox W S E N`` option."""
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), required=True
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — dispatch to one subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="list what Mapterhorn publishes")
    p_probe.add_argument("--origin", default=DEFAULT_ORIGIN)

    p_select = sub.add_parser("select", help="write the clip file and credits")
    p_select.add_argument("--coverage", type=Path, required=True)
    p_select.add_argument("--attribution", type=Path, required=True)
    p_select.add_argument("--area", type=Path, required=True)
    _add_bbox(p_select)
    p_select.add_argument("--clip", type=Path, required=True)
    p_select.add_argument("--credits", type=Path, required=True)
    p_select.add_argument(
        "--source-field",
        action="append",
        help="coverage property holding the source id (repeatable)",
    )

    p_manifest = sub.add_parser("manifest", help="print the widened manifest")
    p_manifest.add_argument("--swiss", type=Path, required=True)
    p_manifest.add_argument("--area", type=Path, required=True)
    _add_bbox(p_manifest)
    p_manifest.add_argument("--credits", type=Path, required=True)
    p_manifest.add_argument("--dem-manifest", type=Path, required=True)
    p_manifest.add_argument("--dem-version", required=True)

    p_gap = sub.add_parser("verify-gap", help="print verify.sh's coverage file")
    p_gap.add_argument("--clip", type=Path, required=True)
    p_gap.add_argument("--area", type=Path, required=True)
    _add_bbox(p_gap)
    p_gap.add_argument("--swiss", type=Path, required=True, help="swiss manifest")
    p_gap.add_argument(
        "--profile-points", type=Path, required=True, help="terrain_grid output"
    )
    p_gap.add_argument(
        "--profile-swiss",
        type=Path,
        required=True,
        help="gdallocationinfo -valonly of warped.tif at those points",
    )

    p_zoom = sub.add_parser("dem-zoom", help="exit 1 if the copy stops above z14")
    p_zoom.add_argument("dem_manifest", type=Path)

    args = parser.parse_args(argv)
    if args.command == "probe":
        print("\n".join(probe(args.origin)))
    elif args.command == "select":
        clip, credits = select(
            _read(args.coverage),
            _read(args.attribution),
            _read(args.area),
            _bbox(args.bbox),
            tuple(args.source_field) if args.source_field else SOURCE_FIELDS,
        )
        args.clip.write_text(json.dumps(clip), encoding="utf-8")
        args.credits.write_text(json.dumps(credits, indent=2) + "\n", encoding="utf-8")
        names = ", ".join(c["id"] for c in credits["credits"])
        print(
            f"==> {len(credits['credits'])} national models: {names}", file=sys.stderr
        )
    elif args.command == "manifest":
        area_bbox = mapterhorn_area_bbox(_read(args.area), _bbox(args.bbox))
        merged = merge_manifest(
            _read(args.swiss),
            area_bbox,
            _read(args.credits),
            _read(args.dem_manifest),
            args.dem_version,
        )
        print(json.dumps(merged, indent=2))
    elif args.command == "verify-gap":
        print(json.dumps(_verify_gap(args), indent=2))
    else:
        reason = check_dem_zoom(_read(args.dem_manifest))
        if reason:
            print(f"error: {reason}", file=sys.stderr)
            return 1
    return 0


def _verify_gap(args: argparse.Namespace) -> dict[str, Any]:
    """Build verify-gap.json from the CLI's inputs."""
    clip, area = _read(args.clip), _read(args.area)
    swiss = _read(args.swiss)
    gap = find_gap(clip, area, _bbox(args.bbox), swiss["bbox"])

    # terrain_grid.py profile-points: "name<TAB>distance lon lat easting northing".
    samples: dict[str, list[tuple[float, float, float]]] = {}
    for line in args.profile_points.read_text(encoding="utf-8").splitlines():
        name, rest = line.split("\t")
        distance, lon, lat, _, _ = (float(v) for v in rest.split())
        samples.setdefault(name, []).append((distance, lon, lat))
    values = args.profile_swiss.read_text(encoding="utf-8").splitlines()
    valid = [_valid(value) for value in values]
    if len(valid) != sum(len(v) for v in samples.values()):
        raise ValueError("profile-swiss does not hold one value per profile sample")

    profiles: dict[str, Any] = {}
    offset = 0
    for name, points in samples.items():
        flags = valid[offset : offset + len(points)]
        offset += len(points)
        profiles[name] = profile_expectations(clip, area, points, flags)
    return {"gap": gap, "profiles": profiles}


def _valid(value: str) -> bool:
    """Whether a gdallocationinfo value is a height rather than no data."""
    try:
        return float(value) != HEIGHT_NODATA_M
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
