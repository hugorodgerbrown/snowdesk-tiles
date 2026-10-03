"""Tests for cutting the warped class rasters into PNG tiles (SNOW-987).

The cutter is addressing plus one rewrite: which pixels of a flat Mercator
raster become which XYZ tile, and turning the warp's uncovered pixels into the
contract's no-data pixel. Neither fails visibly — a tile cut one row out of
place still decodes to plausible classes — so tiles are read back through the
contract's own PNG reader and ``locate``-style addressing rather than through
the cutter's arithmetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cut_terrain_class_tiles import (  # noqa: E402
    MercatorRaster,
    cut,
    main,
    png,
    raster_name,
)
from terrain_class import (  # noqa: E402
    LEVEL,
    NODATA_CLASS,
    NODATA_PIXEL,
    TerrainClass,
    encode_pixel,
    mercator_extent,
    read_pixel,
    read_png_rgba,
)

BBOX = [7.70, 45.98, 7.80, 46.05]
BBOX_T = (7.70, 45.98, 7.80, 46.05)
ZOOM = 12

# The raster's north-west margin tile is index (0, 0). Tile (1, 1) holds data,
# tile (2, 1) is covered but every kernel was void, and the rest is uncovered.
DATA_TILE = (1, 1)
VOID_TILE = (2, 1)
UNCOVERED_PIXEL = (5, 7)


def pixel_for(px: int, py: int) -> bytes:
    """Return the class pixel the synthetic data tile holds at (px, py)."""
    if (px, py) == UNCOVERED_PIXEL:
        return bytes(4)
    if px == 0:
        return bytes(encode_pixel(372, LEVEL))
    return bytes(encode_pixel(1000 + py, (px % 8) << 5 | (py % 18)))


def build_raster(work: Path, zoom: int = ZOOM) -> Path:
    """Write a synthetic warped RGBA raster for the bbox at one zoom."""
    extent = mercator_extent(*BBOX_T, zoom)
    raster = bytearray(extent.width * extent.height * 4)

    def paint(tile: tuple[int, int], source: bytes | None) -> None:
        for py in range(256):
            for px in range(256):
                gx, gy = tile[0] * 256 + px, tile[1] * 256 + py
                offset = (gy * extent.width + gx) * 4
                pixel = pixel_for(px, py) if source is None else source
                raster[offset : offset + 4] = pixel

    paint(DATA_TILE, None)
    paint(VOID_TILE, bytes(NODATA_PIXEL))
    work.mkdir(parents=True, exist_ok=True)
    path = work / raster_name(zoom)
    path.write_bytes(bytes(raster))
    return path


@pytest.fixture
def cut_level(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    extent = mercator_extent(*BBOX_T, ZOOM)
    raster = MercatorRaster(build_raster(tmp_path / "work"), extent)
    out = tmp_path / "out"
    try:
        level = cut(raster, out)
    finally:
        raster.close()
    return out, level


def tile_path(out: Path, index: tuple[int, int]) -> Path:
    extent = mercator_extent(*BBOX_T, ZOOM)
    x, y = extent.tile_x + index[0], extent.tile_y + index[1]
    return out / str(ZOOM) / str(x) / f"{y}.png"


def test_only_the_tile_with_data_is_written(
    cut_level: tuple[Path, dict[str, object]],
) -> None:
    out, level = cut_level

    assert [path.name for path in out.rglob("*.png")] == [
        tile_path(out, DATA_TILE).name
    ]
    assert tile_path(out, DATA_TILE).exists()
    # Covered, but every pixel void: an empty answer is a 204, not a PNG.
    assert not tile_path(out, VOID_TILE).exists()
    assert level["tile_count"] == 1


def test_the_tile_decodes_to_what_was_warped(
    cut_level: tuple[Path, dict[str, object]],
) -> None:
    body = tile_path(cut_level[0], DATA_TILE).read_bytes()

    assert read_pixel(body, 9, 20) == TerrainClass(1020, 2, "NE")
    assert read_pixel(body, 255, 255) == TerrainClass(1255, 255 % 18, "NW")
    assert read_pixel(body, 0, 3) == TerrainClass(372, 0, None)


def test_an_uncovered_pixel_becomes_nodata_not_transparent(
    cut_level: tuple[Path, dict[str, object]],
) -> None:
    body = tile_path(cut_level[0], DATA_TILE).read_bytes()

    assert read_pixel(body, *UNCOVERED_PIXEL) == NODATA_CLASS
    # And its neighbours were not disturbed by the rewrite.
    assert read_pixel(body, UNCOVERED_PIXEL[0] + 1, UNCOVERED_PIXEL[1]).height_m == (
        1000 + UNCOVERED_PIXEL[1]
    )


def test_every_pixel_is_opaque(cut_level: tuple[Path, dict[str, object]]) -> None:
    width, height, rows = read_png_rgba(tile_path(cut_level[0], DATA_TILE).read_bytes())

    assert (width, height) == (256, 256)
    assert all(row[3::4] == b"\xff" * 256 for row in rows)


def test_the_level_summary(cut_level: tuple[Path, dict[str, object]]) -> None:
    out, level = cut_level
    extent = mercator_extent(*BBOX_T, ZOOM)

    assert level["zoom"] == ZOOM
    assert level["bytes"] == tile_path(out, DATA_TILE).stat().st_size
    assert level["tile_x"] == [extent.tile_x + 1] * 2
    assert level["tile_y"] == [extent.tile_y + 1] * 2


def test_partial_alpha_is_refused(tmp_path: Path) -> None:
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    body[3] = 128  # one half-covered pixel: a warp that was not -r near
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        with pytest.raises(ValueError, match="alpha"):
            raster.tile(0, 0)
    finally:
        raster.close()


def test_a_transparent_pixel_with_colour_becomes_nodata(tmp_path: Path) -> None:
    # What GDAL 3.8 wrote across the uncovered edge of the live z14 warp: the
    # colour kept, the alpha cleared. Two such pixels open the data tile's
    # first row, beside real classes the rewrite must leave alone.
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    start = (DATA_TILE[1] * 256 * extent.width + DATA_TILE[0] * 256) * 4
    body[start + 4 : start + 8] = bytes([0, 0, 255, 0])
    body[start + 8 : start + 12] = bytes([7, 9, 42, 0])
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        rows = raster.tile(*DATA_TILE)
    finally:
        raster.close()

    assert rows is not None
    assert rows[0][4:12] == bytes(NODATA_PIXEL) * 2
    assert rows[0][0:4] == pixel_for(0, 0)
    assert rows[0][12:16] == pixel_for(3, 0)
    assert rows[0][3::4] == b"\xff" * 256


def test_a_nodata_pixel_with_a_height_is_refused(tmp_path: Path) -> None:
    # What the first live z14 warp left behind: a no-data class under a height.
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    start = (DATA_TILE[1] * 256 * extent.width + DATA_TILE[0] * 256) * 4
    body[start + 8 : start + 12] = bytes([8, 144, 255, 255])
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        with pytest.raises(ValueError, match="carries a height"):
            raster.tile(*DATA_TILE)
    finally:
        raster.close()


def test_a_dropped_row_between_covered_rows_is_refused(tmp_path: Path) -> None:
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    row = DATA_TILE[1] * 256 + 40
    start = (row * extent.width + DATA_TILE[0] * 256) * 4
    body[start : start + 256 * 4] = bytes([0, 0, 255, 0]) * 256
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        with pytest.raises(ValueError, match="row 40 is wholly uncovered"):
            raster.tile(*DATA_TILE)
    finally:
        raster.close()


def test_a_dropped_row_on_a_tile_boundary_is_refused(tmp_path: Path) -> None:
    # Row 0 of the data tile, with ground in the tile above it: a gap across
    # the boundary, not the edge of coverage.
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    above = DATA_TILE[1] * 256 - 1
    start = (above * extent.width + DATA_TILE[0] * 256) * 4
    body[start : start + 256 * 4] = bytes(encode_pixel(1500, 3)) * 256
    row = DATA_TILE[1] * 256
    start = (row * extent.width + DATA_TILE[0] * 256) * 4
    body[start : start + 256 * 4] = bytes([0, 0, 255, 0]) * 256
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        with pytest.raises(ValueError, match="row 0 is wholly uncovered"):
            raster.tile(*DATA_TILE)
    finally:
        raster.close()


def test_uncovered_rows_at_a_tile_edge_are_not_a_gap(tmp_path: Path) -> None:
    # Coverage that starts part way down a tile is an edge, not a dropped row.
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = build_raster(tmp_path)
    body = bytearray(path.read_bytes())
    for row in range(DATA_TILE[1] * 256, DATA_TILE[1] * 256 + 30):
        start = (row * extent.width + DATA_TILE[0] * 256) * 4
        body[start : start + 256 * 4] = bytes(256 * 4)
    path.write_bytes(bytes(body))

    raster = MercatorRaster(path, extent)
    try:
        rows = raster.tile(*DATA_TILE)
    finally:
        raster.close()

    assert rows is not None
    assert rows[0][:4] == bytes(NODATA_PIXEL)


def test_a_raster_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "short.raw"
    path.write_bytes(b"\0" * 16)

    with pytest.raises(ValueError, match="RGBA raster"):
        MercatorRaster(path, mercator_extent(*BBOX_T, ZOOM))


def test_an_empty_level_is_an_error(tmp_path: Path) -> None:
    extent = mercator_extent(*BBOX_T, ZOOM)
    path = tmp_path / raster_name(ZOOM)
    path.write_bytes(bytes(extent.width * extent.height * 4))

    raster = MercatorRaster(path, extent)
    try:
        with pytest.raises(RuntimeError, match="no z12 tile"):
            cut(raster, tmp_path / "out")
    finally:
        raster.close()


def test_png_is_rgba_with_filter_zero() -> None:
    body = png([bytes(NODATA_PIXEL) * 3] * 2)

    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert read_png_rgba(body) == (3, 2, [bytes(NODATA_PIXEL) * 3] * 2)


def run_main(tmp_path: Path, zooms: list[int]) -> Path:
    """Build rasters for ``zooms``, run the cutter over them, return its out dir."""
    work = tmp_path / "work"
    for zoom in zooms:
        build_raster(work, zoom)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"bbox": BBOX}))
    out = tmp_path / "terrain-class"

    args = ["--work", str(work), "--manifest", str(manifest), "--out", str(out)]
    args += ["--origin", "https://o.test", "--version", "v1", "--zooms"]
    assert main(args + [str(zoom) for zoom in zooms]) == 0
    return out


def test_main_writes_tiles_and_the_descriptor(tmp_path: Path) -> None:
    out = run_main(tmp_path, [12, 13, 14])

    doc = json.loads((out / "tiles.json").read_text())
    pngs = sorted(out.rglob("*.png"))
    assert doc["tiles"] == ["https://o.test/terrain-class/v1/{z}/{x}/{y}.png"]
    assert doc["bounds"] == BBOX
    assert doc["tile_count"] == len(pngs) == 3
    assert doc["bytes"] == sum(path.stat().st_size for path in pngs)
    assert [level["zoom"] for level in doc["levels"]] == [12, 13, 14]
    assert doc["encoding"]["nodata"] == 255


def test_a_partial_build_writes_no_descriptor(tmp_path: Path) -> None:
    out = run_main(tmp_path, [12, 13, 14])
    out = run_main(tmp_path, [14])

    assert not (out / "tiles.json").exists()
    assert sorted(out.rglob("*.png"))


def test_a_rebuild_drops_a_tile_that_became_empty(tmp_path: Path) -> None:
    out = run_main(tmp_path, [12, 13, 14])
    stale = out / "12" / "999999" / "0.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    run_main(tmp_path, [12, 13, 14])

    assert not stale.exists()


def test_main_refuses_a_zoom_the_worker_will_not_serve(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["--work", str(tmp_path), "--manifest", "m", "--out", str(tmp_path)]
            + ["--zooms", "11"]
        )


# --- End to end, when GDAL is installed -------------------------------------


@pytest.mark.skipif(shutil.which("gdalwarp") is None, reason="GDAL not installed")
def test_build_terrain_class_end_to_end(tmp_path: Path) -> None:
    """Run the real build over synthetic ground and compare pixels with the grid.

    The one test that exercises gdalwarp: the VRT's georeferencing, nearest
    resampling, the exact transformer, the alpha band and the BIP layout all
    have to be right for a pixel to land on the cell ``expected_class`` names.
    It is the test-suite version of what verify.sh does against the live origin.
    """
    np = pytest.importorskip("numpy")
    from cut_terrain_tiles import Raster
    from cut_terrain_tiles import cut as cut_elevation
    from terrain_class import describe, expected_class, locate
    from terrain_grid import (
        CELL_SIZE_M,
        NODATA,
        TILE_CELLS,
        lonlat_extent,
        snap_extent,
    )

    extent = snap_extent(*lonlat_extent(*BBOX_T))
    cols = (extent.east - extent.west) // CELL_SIZE_M
    rows = (extent.north - extent.south) // CELL_SIZE_M
    row_index, col_index = np.mgrid[0:rows, 0:cols].astype(np.float64)
    metres = 2400.0 + 300.0 * np.sin(row_index / 37.0) * np.cos(col_index / 53.0)
    stored = np.round(metres / 0.25).astype("<i2")
    # The margin ring holds no ground, as it does in a real build.
    stored[:TILE_CELLS] = stored[-TILE_CELLS:] = NODATA
    stored[:, :TILE_CELLS] = stored[:, -TILE_CELLS:] = NODATA

    terrain_work = tmp_path / "work" / "terrain"
    terrain_work.mkdir(parents=True)
    (terrain_work / "grid.raw").write_bytes(stored.tobytes())
    (terrain_work / "manifest.json").write_text(json.dumps({"bbox": BBOX}))

    env = {
        **os.environ,
        "PYTHON": sys.executable,
        "TERRAIN_WORK_DIR": str(terrain_work),
        "TERRAIN_CLASS_WORK_DIR": str(tmp_path / "work" / "terrain-class"),
        "DIST_DIR": str(tmp_path / "dist"),
        # One tile row per strip, so every zoom is warped in several strips and
        # the join is exercised, not just a single-strip copy.
        "TERRAIN_CLASS_STRIP_TILES": "1",
    }
    subprocess.run(  # noqa: S603
        ["/bin/bash", str(ROOT / "scripts" / "build-terrain-class.sh")],
        env=env,
        cwd=ROOT,
        check=True,
    )
    out = tmp_path / "dist" / "terrain-class"
    assert json.loads((out / "tiles.json").read_text())["tile_count"] > 0

    # The elevation tiles the same grid would publish, read back the way the
    # live origin serves them: absent is None, which is a 204.
    elevation = tmp_path / "terrain-tiles"
    raster = Raster(terrain_work / "grid.raw", extent.west, extent.north, cols, rows)
    try:
        cut_elevation(raster, elevation)
    finally:
        raster.close()

    def fetch(tile_x: int, tile_y: int) -> bytes | None:
        path = elevation / str(tile_x) / f"{tile_y}.s16"
        return path.read_bytes() if path.exists() else None

    west, south, east, north = BBOX_T
    for step_x in range(5):
        for step_y in range(5):
            lon = west + (east - west) * (step_x + 0.5) / 5
            lat = south + (north - south) * (step_y + 0.5) / 5
            zoom, tile_x, tile_y, px, py = locate(lon, lat, 14)
            tile = out / str(zoom) / str(tile_x) / f"{tile_y}.png"
            got = describe(read_pixel(tile.read_bytes(), px, py))
            assert got == expected_class(lon, lat, 14, fetch), (lon, lat)
