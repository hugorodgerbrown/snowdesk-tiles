"""Tests for the Swiss-heights-unchanged check (SNOW-1505).

The check is the stop before upload: a rebuilt grid that broke the Swiss
heights must not publish, while one that rounds them a quarter-metre
differently, or carries newer swisstopo surveys, may. So each test builds two
small tile trees and changes one thing between them.
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
    FAR_STEPS,
    HeightChangedError,
    Tally,
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

    count, tally = compare(ref, cand)

    assert count == 2
    assert tally.valid == 2 * CELLS
    assert tally.changed == 0
    assert tally.failures() == []


def test_quarter_metre_rounding_everywhere_passes(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    write(cand, 3239, 2005, [v + (1 if i % 2 else -1) for i, v in enumerate(ramp())])

    _, tally = compare(ref, cand)

    assert tally.changed == CELLS
    assert tally.far == 0
    assert tally.failures() == []


def test_a_few_newer_surveys_pass_and_the_largest_is_named(
    trees: tuple[Path, Path],
) -> None:
    ref, cand = trees
    values = ramp()
    values[5 * STORED_CELLS + 7] += 427  # 107 m: a quarry
    write(cand, 3239, 2005, values)

    _, tally = compare(ref, cand)

    assert tally.far == 1
    assert tally.failures() == []
    assert "+106.75 m at tile 3239/2005 cell (row 5, col 7)" in tally.summary()


def test_more_than_one_percent_moved_far_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    values = ramp()
    for index in range(0, CELLS, 50):  # 2% of one tile is 1% of both
        values[index] += FAR_STEPS + 1
    values[1] += FAR_STEPS + 1
    write(cand, 3239, 2005, values)

    _, tally = compare(ref, cand)

    assert any("moved more than 2 m" in problem for problem in tally.failures())


def test_a_systematic_shift_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    for x in (3238, 3239):
        write(cand, x, 2005, [v + 2 for v in ramp()])  # +0.5 m everywhere

    _, tally = compare(ref, cand)

    assert tally.far == 0
    assert any("shifted by +0.50 m" in problem for problem in tally.failures())


def test_a_cell_absent_in_the_reference_may_gain_a_height(
    trees: tuple[Path, Path],
) -> None:
    ref, cand = trees
    values = ramp()
    values[100] = NODATA
    write(ref, 3238, 2005, values)

    _, tally = compare(ref, cand)

    assert tally.failures() == []


def test_a_lost_stretch_of_heights_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    values = ramp()
    for index in range(STORED_CELLS):  # one row of cells gone
        values[index] = NODATA
    write(cand, 3238, 2005, values)

    _, tally = compare(ref, cand)

    assert tally.lost == STORED_CELLS
    assert any("lost their height" in problem for problem in tally.failures())


def test_one_lost_height_is_tolerated() -> None:
    tally = Tally()
    tally.valid, tally.lost = 16_000_000, 4

    assert tally.failures() == []


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

    count, tally = compare(ref, cand)

    assert count == 2
    assert tally.failures() == []


def test_a_truncated_candidate_tile_fails(trees: tuple[Path, Path]) -> None:
    ref, cand = trees
    (cand / "3238" / "2005.s16").write_bytes(body(ramp())[:-2])

    with pytest.raises(HeightChangedError, match="bytes"):
        compare(ref, cand)


def test_an_empty_reference_fails(tmp_path: Path) -> None:
    with pytest.raises(HeightChangedError, match="no tiles"):
        compare(tmp_path, tmp_path)


def test_cli_prints_the_summary_and_exits_non_zero_beyond_tolerance(
    trees: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    ref, cand = trees
    assert main([str(ref), str(cand)]) == 0
    assert "within tolerance" in capsys.readouterr().err

    write(cand, 3238, 2005, [v + 40 for v in ramp()])

    assert main([str(ref), str(cand)]) == 1
    err = capsys.readouterr().err
    assert "2 tiles," in err
    assert "error: 50.00% of Swiss cells moved more than 2 m" in err


def test_cli_exits_non_zero_on_a_missing_tile(
    trees: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    ref, cand = trees
    (cand / "3239" / "2005.s16").unlink()

    assert main([str(ref), str(cand)]) == 1
    assert "not the candidate" in capsys.readouterr().err


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
