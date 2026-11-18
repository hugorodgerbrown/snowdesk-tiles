"""Tests for the Swiss-heights-unchanged check (SNOW-1505).

The check is the stop before upload: a rebuilt grid that moved one Swiss
height by a quarter-metre must not publish. So each test builds two small tile
trees and changes exactly one thing between them.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compare_grids import (  # noqa: E402
    HeightChangedError,
    classify,
    compare,
    main,
    sample,
)
from terrain_grid import NODATA, SKIRT_CELLS, STORED_CELLS  # noqa: E402

CELLS = STORED_CELLS * STORED_CELLS


def body(values: list[int]) -> bytes:
    return struct.pack(f"<{CELLS}h", *values)


def ramp() -> list[int]:
    """Every cell valid, every cell different."""
    return [index % 30000 for index in range(CELLS)]


def write(root: Path, x: int, y: int, values: list[int]) -> None:
    (root / str(x)).mkdir(parents=True, exist_ok=True)
    (root / str(x) / f"{y}.s16").write_bytes(body(values))


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    ref, cand = tmp_path / "ref", tmp_path / "cand"
    for root in (ref, cand):
        write(root, 3238, 2005, ramp())
        write(root, 3239, 2005, ramp())
    return ref, cand


def test_identical_trees_pass(trees: tuple[Path, Path]) -> None:
    ref, cand = trees

    assert compare(ref, cand) == 2


def test_one_changed_valid_cell_fails_naming_the_tile_and_cell(
    trees: tuple[Path, Path],
) -> None:
    ref, cand = trees
    values = ramp()
    values[5 * STORED_CELLS + 7] += 1  # a quarter-metre
    write(cand, 3239, 2005, values)

    with pytest.raises(
        HeightChangedError, match=r"tile 3239/2005 cell \(row 5, col 7\)"
    ):
        compare(ref, cand)


def test_a_cell_absent_in_the_reference_may_gain_a_height(
    trees: tuple[Path, Path],
) -> None:
    ref, cand = trees
    values = ramp()
    values[100] = NODATA
    write(ref, 3238, 2005, values)

    assert compare(ref, cand) == 2


def test_a_height_lost_from_the_reference_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    values = ramp()
    values[100] = NODATA
    write(cand, 3238, 2005, values)

    with pytest.raises(HeightChangedError, match="candidate -32768"):
        compare(ref, cand)


def test_a_tile_missing_from_the_candidate_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    (cand / "3239" / "2005.s16").unlink()

    with pytest.raises(HeightChangedError, match="not the candidate"):
        compare(ref, cand)


def test_a_tile_only_the_candidate_has_is_new_coverage(
    trees: tuple[Path, Path],
) -> None:
    ref, cand = trees
    write(cand, 4000, 2000, ramp())

    assert compare(ref, cand) == 2


def test_a_truncated_candidate_tile_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    (cand / "3238" / "2005.s16").write_bytes(body(ramp())[:-2])

    with pytest.raises(HeightChangedError, match="bytes"):
        compare(ref, cand)


def test_an_empty_reference_fails(tmp_path: Path) -> None:
    with pytest.raises(HeightChangedError, match="no tiles"):
        compare(tmp_path, tmp_path)


def test_cli_exits_non_zero_on_a_difference(
    trees: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    ref, cand = trees
    assert main([str(ref), str(cand)]) == 0

    values = ramp()
    values[0] = 1
    write(cand, 3238, 2005, values)

    assert main([str(ref), str(cand)]) == 1
    assert "tile 3238/2005 cell (row 0, col 0)" in capsys.readouterr().err


# --- The reference sample ------------------------------------------------------


def border_tile() -> list[int]:
    values = ramp()
    values[SKIRT_CELLS * STORED_CELLS + SKIRT_CELLS] = NODATA
    return values


def test_a_tile_with_a_gap_in_its_data_is_a_border_tile() -> None:
    skirt_only = ramp()
    skirt_only[0] = NODATA  # the skirt does not count

    assert classify(body(ramp())) == "interior"
    assert classify(body(skirt_only)) == "interior"
    assert classify(body(border_tile())) == "border"


def fake_origin() -> dict[str, bytes | None]:
    grid = {
        "sources": [
            {"id": "swissalti3d", "coverage": {"tile_x": [0, 4], "tile_y": [0, 3]}}
        ]
    }
    bodies: dict[str, bytes | None] = {
        "https://t.example/terrain/v1/grid.json": json.dumps(grid).encode()
    }
    for x in range(5):
        for y in range(4):
            url = f"https://t.example/terrain/v1/{x}/{y}.s16"
            if x == 4:
                bodies[url] = None  # outside coverage: 204
            elif y == 0:
                bodies[url] = body(border_tile())
            else:
                bodies[url] = body(ramp())
    return bodies


def test_the_sample_is_seeded_and_fills_both_quotas(tmp_path: Path) -> None:
    bodies = fake_origin()

    first = sample(
        "https://t.example/terrain/v1/", tmp_path / "a", 1505, 5, 2, bodies.__getitem__
    )
    again = sample(
        "https://t.example/terrain/v1", tmp_path / "b", 1505, 5, 2, bodies.__getitem__
    )

    assert first["interior"] == 5
    assert first["border"] == 2
    assert sorted((tmp_path / "a").rglob("*.s16")) != []
    assert [
        p.relative_to(tmp_path / "a") for p in sorted((tmp_path / "a").rglob("*.s16"))
    ] == [
        p.relative_to(tmp_path / "b") for p in sorted((tmp_path / "b").rglob("*.s16"))
    ]
    assert again == first


def test_a_sample_that_cannot_fill_its_quota_fails(tmp_path: Path) -> None:
    bodies = fake_origin()

    with pytest.raises(ValueError, match="border tiles"):
        sample("https://t.example/terrain/v1", tmp_path, 1, 1, 10, bodies.__getitem__)


def test_the_sample_reads_the_swissalti3d_coverage_only(tmp_path: Path) -> None:
    bodies = {"https://t.example/grid.json": b'{"sources": [{"id": "mapterhorn"}]}'}

    with pytest.raises(ValueError, match="no swissalti3d"):
        sample("https://t.example", tmp_path, 1, 1, 1, bodies.__getitem__)
