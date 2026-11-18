#!/usr/bin/env python3
r"""Prove a rebuilt terrain grid kept every height the reference grid had.

SNOW-1505. The v2 grid adds the rest of the Alps around swissALTI3D; inside
swissALTI3D's coverage it must hold exactly the heights v1 published, to the
stored quarter-metre. Nothing downstream would notice otherwise — a Swiss slope
recomputed from slightly different heights is still a plausible slope — so the
build compares before it uploads, and a difference stops the upload.

The rule, for every tile in the reference tree:

* the candidate tree must hold the tile too (a missing tile fails);
* every cell valid in the reference (not -32768) must hold the same Int16 in
  the candidate;
* a cell absent in the reference may gain a height — that is the new coverage.

It exits non-zero on the first difference, naming the tile and the cell, so the
build log says where to look.

``sample`` fetches the reference: tiles from the live grid, picked by a seeded
shuffle of the swissALTI3D coverage rectangle in its grid.json, kept until it
holds the asked-for number of interior tiles (every data cell valid) and border
tiles (some valid, some not). The same seed picks the same tiles.

Stdlib only.

Usage:
    python scripts/compare_grids.py REF CAND
    python scripts/compare_grids.py sample --url https://tiles.../terrain/v1 \
        --out work/terrain/reference --seed 1505 --interior 200 --border 100
"""

from __future__ import annotations

import argparse
import json
import random
import struct
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from terrain_grid import NODATA, SKIRT_CELLS, STORED_CELLS, TILE_BYTES, TILE_CELLS

CELLS = STORED_CELLS * STORED_CELLS
_FORMAT = f"<{CELLS}h"


class HeightChangedError(Exception):
    """The candidate does not hold a height the reference has."""


def decode(body: bytes, label: str) -> tuple[int, ...]:
    """Return a tile body's stored Int16 cells, refusing a body of the wrong size."""
    if len(body) != TILE_BYTES:
        raise HeightChangedError(f"{label} is {len(body)} bytes, expected {TILE_BYTES}")
    cells: tuple[int, ...] = struct.unpack(_FORMAT, body)
    return cells


def compare_tile(reference: bytes, candidate: bytes, label: str) -> None:
    """Raise at the first valid reference cell the candidate changed."""
    ref = decode(reference, f"reference {label}")
    cand = decode(candidate, f"candidate {label}")
    if ref == cand:
        return
    for index, (old, new) in enumerate(zip(ref, cand, strict=True)):
        if old != NODATA and old != new:
            row, col = divmod(index, STORED_CELLS)
            raise HeightChangedError(
                f"tile {label} cell (row {row}, col {col}): reference {old}, "
                f"candidate {new}"
            )


def tiles(root: Path) -> Iterator[tuple[int, int]]:
    """Yield every (x, y) under a tile tree laid out ``<x>/<y>.s16``, in order."""
    found = []
    for path in root.glob("*/*.s16"):
        found.append((int(path.parent.name), int(path.stem)))
    yield from sorted(found)


def compare(reference: Path, candidate: Path) -> int:
    """Compare two tile trees; return the number of tiles compared."""
    count = 0
    for x, y in tiles(reference):
        label = f"{x}/{y}"
        other = candidate / str(x) / f"{y}.s16"
        if not other.is_file():
            raise HeightChangedError(
                f"tile {label} is in the reference but not the candidate"
            )
        compare_tile(
            (reference / str(x) / f"{y}.s16").read_bytes(), other.read_bytes(), label
        )
        count += 1
    if not count:
        raise HeightChangedError(f"the reference {reference} holds no tiles")
    return count


# --- sample -----------------------------------------------------------------


def classify(body: bytes) -> str:
    """Return ``interior`` if every data cell holds a height, else ``border``."""
    cells = decode(body, "sample")
    for row in range(SKIRT_CELLS, SKIRT_CELLS + TILE_CELLS):
        start = row * STORED_CELLS + SKIRT_CELLS
        if NODATA in cells[start : start + TILE_CELLS]:
            return "border"
    return "interior"


def fetch_tile(url: str) -> bytes | None:
    """Return a tile body, or None for the 204 outside coverage."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to fetch non-HTTP url: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"User-Agent": "snowdesk-tiles"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - checked
        if response.status == 204:
            return None
        body: bytes = response.read()
    return body


def swiss_coverage(grid: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Return swissALTI3D's (tile_x, tile_y) ranges from a grid.json."""
    for source in grid.get("sources", []):
        if source.get("id") == "swissalti3d":
            coverage = source["coverage"]
            return list(coverage["tile_x"]), list(coverage["tile_y"])
    raise ValueError("grid.json has no swissalti3d source")


def sample(
    url: str,
    out: Path,
    seed: int,
    interior: int,
    border: int,
    fetch: Callable[[str], bytes | None] = fetch_tile,
) -> dict[str, int]:
    """Fetch a seeded sample of interior and border tiles into ``out``.

    Raises if the coverage rectangle runs out before both quotas are met: a
    sample smaller than asked for is a weaker check, not the same one.
    """
    base = url.rstrip("/")
    grid_body = fetch(f"{base}/grid.json")
    if grid_body is None:
        raise ValueError(f"{base}/grid.json is empty")
    (x0, x1), (y0, y1) = swiss_coverage(json.loads(grid_body))
    candidates = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    random.Random(seed).shuffle(candidates)  # noqa: S311 - a sample, not a secret

    wanted = {"interior": interior, "border": border}
    kept = {"interior": 0, "border": 0}
    fetched = 0
    for x, y in candidates:
        if kept == wanted:
            break
        body = fetch(f"{base}/{x}/{y}.s16")
        fetched += 1
        if body is None:
            continue
        kind = classify(body)
        if kept[kind] >= wanted[kind]:
            continue
        column = out / str(x)
        column.mkdir(parents=True, exist_ok=True)
        (column / f"{y}.s16").write_bytes(body)
        kept[kind] += 1
    if kept != wanted:
        raise ValueError(
            f"the coverage held {kept['interior']} interior and {kept['border']} "
            f"border tiles; {interior} and {border} were asked for"
        )
    return kept | {"fetched": fetched}


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — compare two trees, or fetch a reference sample."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["sample"]:
        parser = argparse.ArgumentParser(description="fetch a reference sample")
        parser.add_argument("--url", required=True, help="terrain/<version> base URL")
        parser.add_argument("--out", type=Path, required=True)
        parser.add_argument("--seed", type=int, required=True)
        parser.add_argument("--interior", type=int, default=200)
        parser.add_argument("--border", type=int, default=100)
        args = parser.parse_args(argv[1:])
        try:
            summary = sample(args.url, args.out, args.seed, args.interior, args.border)
        except (ValueError, urllib.error.URLError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(
            f"==> {summary['interior']} interior and {summary['border']} border "
            f"tiles from {summary['fetched']} fetched",
            file=sys.stderr,
        )
        return 0

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    try:
        count = compare(args.reference, args.candidate)
    except HeightChangedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"==> {count} tiles: every reference height unchanged", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
