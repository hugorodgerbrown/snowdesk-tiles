#!/usr/bin/env python3
r"""Prove a rebuilt terrain grid kept the heights the reference grid had.

SNOW-1505. The v2 grid adds the rest of the Alps around swissALTI3D; inside
swissALTI3D's coverage it should hold the heights v1 published. Nothing
downstream would notice otherwise — a Swiss slope recomputed from wrong heights
is still a plausible slope — so the build compares before it uploads, and a
failure stops the upload.

Not to the quarter-metre. The first Alps-wide build (19 Nov 2026) changed 15.5%
of 16.1 million sampled Swiss cells: 14.6% by one stored step (0.25 m), the
rounding of a warp over a wider grid, and about 0.1% by more than 2 m, up to
107 m, where swisstopo had published newer surveys since v1 (quarries,
landslides, glaciers). Hugo chose to publish those newer heights. What the check
still catches is a build that broke them: a composite that let the fill
overwrite Switzerland, an offset, a flip, a lost stretch of coverage.

The rule, over every tile in the reference tree:

* the candidate tree must hold the tile too, at the right size;
* at most MAX_FAR_SHARE of the reference's valid cells may move by more than
  FAR_STEPS stored steps (2 m);
* at most MAX_LOST_SHARE of them may lose their height;
* the mean change may not exceed MAX_MEAN_STEPS (a systematic shift);
* a cell absent in the reference may gain a height — that is the new coverage.

It always prints the summary, and names the largest change, so the log says
where to look.

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

from stamp import log
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


#: A change of more than this many stored steps (0.25 m each) is a "far" one.
FAR_STEPS = 8
#: At most this share of valid reference cells may move far: 1%, ten times what
#: newer swisstopo surveys moved in the first Alps-wide build (0.1%).
MAX_FAR_SHARE = 0.01
#: At most this share may lose their height (4 of 16.1 million did, 0.00002%).
MAX_LOST_SHARE = 0.0001
#: The mean change, in stored steps, may not exceed this either way.
MAX_MEAN_STEPS = 1.0


class Tally:
    """What changed between the reference and the candidate, over valid cells."""

    def __init__(self) -> None:
        """Start empty."""
        self.valid = 0
        self.changed = 0
        self.far = 0
        self.lost = 0
        self.total_steps = 0
        self.largest = 0
        self.largest_at = ""

    def add_tile(self, reference: bytes, candidate: bytes, label: str) -> None:
        """Count one tile's valid reference cells and how each one changed."""
        ref = decode(reference, f"reference {label}")
        cand = decode(candidate, f"candidate {label}")
        for index, (old, new) in enumerate(zip(ref, cand, strict=True)):
            if old == NODATA:
                continue
            self.valid += 1
            if new == NODATA:
                self.lost += 1
                continue
            step = new - old
            if not step:
                continue
            self.changed += 1
            self.total_steps += step
            if abs(step) > FAR_STEPS:
                self.far += 1
            if abs(step) > abs(self.largest):
                row, col = divmod(index, STORED_CELLS)
                self.largest = step
                self.largest_at = f"tile {label} cell (row {row}, col {col})"

    def failures(self) -> list[str]:
        """Return why the candidate fails, or nothing when it passes."""
        if not self.valid:
            return ["the reference holds no valid cells"]
        problems = []
        if self.far > MAX_FAR_SHARE * self.valid:
            problems.append(
                f"{self.far / self.valid:.2%} of Swiss cells moved more than "
                f"{FAR_STEPS * 0.25:g} m (at most {MAX_FAR_SHARE:.0%} may)"
            )
        if self.lost > MAX_LOST_SHARE * self.valid:
            problems.append(
                f"{self.lost} Swiss cells lost their height "
                f"(at most {MAX_LOST_SHARE:.2%} may)"
            )
        mean = self.total_steps / self.valid
        if abs(mean) > MAX_MEAN_STEPS:
            problems.append(
                f"the heights shifted by {mean * 0.25:+.2f} m on average "
                f"(at most {MAX_MEAN_STEPS * 0.25:g} m either way)"
            )
        return problems

    def summary(self) -> str:
        """Return the counts, as the build log prints them."""
        mean_m = self.total_steps / self.valid * 0.25 if self.valid else 0.0
        largest = (
            f"; largest {self.largest * 0.25:+.2f} m at {self.largest_at}"
            if self.largest
            else ""
        )
        return (
            f"{self.valid} Swiss cells: {self.changed} changed, {self.far} by more "
            f"than {FAR_STEPS * 0.25:g} m, {self.lost} lost; mean change "
            f"{mean_m:+.3f} m{largest}"
        )


def tiles(root: Path) -> Iterator[tuple[int, int]]:
    """Yield every (x, y) under a tile tree laid out ``<x>/<y>.s16``, in order."""
    found = []
    for path in root.glob("*/*.s16"):
        found.append((int(path.parent.name), int(path.stem)))
    yield from sorted(found)


def compare(reference: Path, candidate: Path) -> tuple[int, Tally]:
    """Compare two tile trees; return the tiles compared and what changed.

    Raises on a tile the candidate lacks or holds at the wrong size, and on an
    empty reference. Whether the changes are acceptable is ``Tally.failures``.
    """
    count = 0
    tally = Tally()
    for x, y in tiles(reference):
        label = f"{x}/{y}"
        other = candidate / str(x) / f"{y}.s16"
        if not other.is_file():
            raise HeightChangedError(
                f"tile {label} is in the reference but not the candidate"
            )
        tally.add_tile(
            (reference / str(x) / f"{y}.s16").read_bytes(), other.read_bytes(), label
        )
        count += 1
    if not count:
        raise HeightChangedError(f"the reference {reference} holds no tiles")
    return count, tally


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
            log(f"error: {error}")
            return 1
        log(
            f"==> {summary['interior']} interior and {summary['border']} border "
            f"tiles from {summary['fetched']} fetched",
        )
        return 0

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    try:
        count, tally = compare(args.reference, args.candidate)
    except HeightChangedError as error:
        log(f"error: {error}")
        return 1
    log(f"==> {count} tiles, {tally.summary()}")
    problems = tally.failures()
    for problem in problems:
        log(f"error: {problem}")
    if problems:
        return 1
    log("==> Swiss heights within tolerance of the reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
