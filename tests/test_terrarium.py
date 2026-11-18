"""Tests for decoding and mosaicking the Terrarium tiles (SNOW-1505).

What the terrain grid takes from Mapterhorn is decided here: which z14 tiles
are fetched, what height a pixel decodes to, and where the decoded block sits
on the Web Mercator raster GDAL warps. Each fails silently downstream — a
wrong decode is a plausible height, a wrong geotransform is the Alps moved —
so each is pinned against numbers worked out by hand.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - parses XML this module wrote
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from terrain_grid import HEIGHT_NODATA_M  # noqa: E402
from terrarium import (  # noqa: E402
    MERCATOR_HALF,
    NODATA_M,
    decode_terrarium,
    decode_tiles,
    lonlat_to_tile,
    main,
    read_tile,
    tile_lonlat_bounds,
    tile_path,
    tile_size_m,
    tile_to_lonlat,
    tiles_bbox,
    tiles_for_area,
    tiles_for_box,
)

TILE_PX = 4  # small tiles keep the fixtures readable; the maths is the same


def box_area(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """Return a one-feature FeatureCollection holding a rectangle."""
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}}
        ],
    }


def encode(metres: float) -> tuple[int, int, int]:
    """Return the Terrarium RGB of a height, as an encoder would write it."""
    value = metres + 32768.0
    red = int(value // 256)
    green = int(value - red * 256)
    blue = round((value - red * 256 - green) * 256)
    return red, green, blue


def write_tile(path: Path, metres: float, size: int = TILE_PX) -> None:
    """Write a lossless WebP tile of one height everywhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (size, size), encode(metres)).save(path, "WEBP", lossless=True)


# --- Terrarium maths ---------------------------------------------------------


@pytest.mark.parametrize(
    ("rgb", "metres"),
    [
        ((128, 0, 0), 0.0),  # sea level
        ((128, 1, 128), 1.5),
        ((143, 4, 32), 3844.125),  # z14's 0.125 m step survives the decode
        ((127, 255, 0), -1.0),
        ((140, 24, 64), 3096.25),
    ],
)
def test_terrarium_decodes_known_pixels(
    rgb: tuple[int, int, int], metres: float
) -> None:
    pixel = np.array([[rgb]], dtype=np.uint8)

    assert decode_terrarium(pixel)[0, 0] == pytest.approx(metres, abs=1e-3)


def test_an_all_black_pixel_is_no_data_not_a_trench() -> None:
    # (0, 0, 0) decodes to -32768 m, below anything the grid can store; read as
    # a height it would be a hole eight kilometres deep in the slope kernel.
    pixel = np.zeros((1, 1, 3), dtype=np.uint8)

    assert decode_terrarium(pixel)[0, 0] == NODATA_M


def test_decoded_no_data_is_the_grids_float_sentinel() -> None:
    # One -dstnodata for both warps depends on it.
    assert NODATA_M == HEIGHT_NODATA_M


def test_a_lossless_webp_round_trips_through_read_tile(tmp_path: Path) -> None:
    path = tmp_path / "t.webp"
    write_tile(path, 2962.5)

    heights = read_tile(path, TILE_PX)

    assert heights is not None
    assert heights.dtype == np.float32
    assert np.all(heights == pytest.approx(2962.5))


def test_a_missing_or_empty_tile_is_no_data(tmp_path: Path) -> None:
    # An empty file is what the fetch leaves for a 204.
    empty = tmp_path / "empty.webp"
    empty.write_bytes(b"")

    assert read_tile(tmp_path / "absent.webp") is None
    assert read_tile(empty) is None


def test_a_tile_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.webp"
    write_tile(path, 100.0, size=8)

    with pytest.raises(ValueError, match="px square"):
        read_tile(path, TILE_PX)


# --- Which tiles -------------------------------------------------------------


def test_listing_takes_the_tiles_whose_centres_are_inside() -> None:
    # A z12 tile at the centre of the area holds exactly sixteen z14 tiles.
    x12, y12 = 2134, 1450
    area: dict[str, Any] = {
        "type": "Polygon",
        "coordinates": [_tile_ring(x12, y12, 12)],
    }

    tiles = tiles_for_area(area, 14)

    assert tiles == sorted(
        (x12 * 4 + dx, y12 * 4 + dy) for dx in range(4) for dy in range(4)
    )


def test_box_listing_takes_a_tile_whose_centre_is_outside() -> None:
    # A box well inside one z14 tile, away from its centre.
    west, south, east, north = tile_lonlat_bounds(8631, 5736, 14)
    box = (west + 1e-4, south + 1e-4, west + 2e-3, south + 2e-3)

    assert tiles_for_box(box, 14) == [(8631, 5736)]
    assert tiles_for_area(box_area(*box), 14) == []


def test_box_listing_takes_every_tile_it_straddles() -> None:
    _, _, east, north = tile_lonlat_bounds(8631, 5736, 14)
    box = (east - 1e-3, north - 1e-3, east + 1e-3, north + 1e-3)

    assert tiles_for_box(box, 14) == [
        (8631, 5735),
        (8631, 5736),
        (8632, 5735),
        (8632, 5736),
    ]


def test_listing_honours_the_box() -> None:
    area = box_area(7.0, 45.0, 8.0, 46.0)
    everything = tiles_for_area(area, 10)

    clipped = tiles_for_area(area, 10, (7.0, 45.0, 7.3, 46.0))

    assert clipped
    assert set(clipped) < set(everything)
    assert all(lonlat_to_tile(7.3, 45.5, 10)[0] > x for x, _ in clipped)


def test_listing_leaves_a_hole_out() -> None:
    outer = _tile_ring(530, 360, 10, span=3)
    hole = _tile_ring(531, 361, 10)
    area = {"type": "Polygon", "coordinates": [outer, hole]}

    tiles = tiles_for_area(area, 10)

    assert len(tiles) == 8
    assert (531, 361) not in tiles


def test_tiles_bbox_is_the_tiles_outer_edges() -> None:
    west, south, east, north = tiles_bbox([(0, 0), (1, 1)], 1)

    assert (west, east) == (-180.0, 180.0)
    assert north == pytest.approx(85.0511, abs=1e-4)
    assert south == pytest.approx(-85.0511, abs=1e-4)


def test_list_cli_writes_z_x_y_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    area = tmp_path / "area.geojson"
    area.write_text(
        json.dumps({"type": "Polygon", "coordinates": [_tile_ring(10, 20, 6)]}),
        encoding="utf-8",
    )

    assert main(["list", str(area), "--zoom", "7"]) == 0

    lines = capsys.readouterr().out.split()
    assert lines == ["7/20/40", "7/20/41", "7/21/40", "7/21/41"]


def _tile_ring(x: int, y: int, zoom: int, span: int = 1) -> list[list[float]]:
    """Return the lon/lat ring of a square of whole tiles, as the area holds."""
    west, north = tile_to_lonlat(x, y, zoom)
    east, south = tile_to_lonlat(x + span, y + span, zoom)
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


# --- The mosaic --------------------------------------------------------------


def test_decode_writes_blocks_with_no_data_for_missing_tiles(tmp_path: Path) -> None:
    tile_dir, out = tmp_path / "webp", tmp_path / "blocks"
    write_tile(tile_path(tile_dir, 14, 8, 4), 1000.0)
    write_tile(tile_path(tile_dir, 14, 9, 5), 2000.0)
    # (9, 4) is listed but came back as a 204; (8, 5) was never fetched.
    empty = tile_path(tile_dir, 14, 9, 4)
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")

    summary = decode_tiles(
        [(8, 4), (9, 4), (8, 5), (9, 5)],
        tile_dir,
        out,
        zoom=14,
        block_tiles=2,
        tile_px=TILE_PX,
    )

    block = np.fromfile(out / "4-2.f32", dtype="<f4").reshape(8, 8)
    assert summary["tiles_decoded"] == 2
    assert summary["tiles_missing"] == 2
    assert np.all(block[:4, :4] == 1000.0)  # (8, 4): top-left
    assert np.all(block[:4, 4:] == NODATA_M)  # (9, 4): the 204
    assert np.all(block[4:, :4] == NODATA_M)  # (8, 5): never fetched
    assert np.all(block[4:, 4:] == 2000.0)  # (9, 5): bottom-right


def test_a_block_with_nothing_in_it_is_not_written(tmp_path: Path) -> None:
    tile_dir, out = tmp_path / "webp", tmp_path / "blocks"
    write_tile(tile_path(tile_dir, 14, 0, 0), 5.0)

    summary = decode_tiles(
        [(0, 0), (6, 6)], tile_dir, out, zoom=14, block_tiles=2, tile_px=TILE_PX
    )

    assert summary["blocks"] == 1
    assert not (out / "3-3.f32").exists()


def test_decode_with_nothing_at_all_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="held any heights"):
        decode_tiles([(0, 0)], tmp_path, tmp_path / "out", tile_px=TILE_PX)


def test_the_vrts_carry_the_projection_and_geotransform(tmp_path: Path) -> None:
    tile_dir, out = tmp_path / "webp", tmp_path / "blocks"
    for x, y in [(8530, 5800), (8533, 5803)]:
        write_tile(tile_path(tile_dir, 14, x, y), 3000.0)

    decode_tiles(
        [(8530, 5800), (8533, 5803)],
        tile_dir,
        out,
        zoom=14,
        block_tiles=2,
        tile_px=TILE_PX,
    )

    size = tile_size_m(14)
    mosaic = ET.parse(out / "mosaic.vrt").getroot()  # noqa: S314 - our own file
    gt = [float(v) for v in mosaic.findtext("GeoTransform", "").split(",")]
    assert mosaic.findtext("SRS") == "EPSG:3857"
    # Blocks (4265, 2900) to (4266, 2901): two blocks of two tiles each way.
    assert mosaic.get("rasterXSize") == str(2 * 2 * TILE_PX)
    assert gt[0] == pytest.approx(-MERCATOR_HALF + 8530 * size)
    assert gt[3] == pytest.approx(MERCATOR_HALF - 5800 * size)
    assert gt[1] == pytest.approx(size / TILE_PX)
    assert gt[5] == pytest.approx(-size / TILE_PX)
    band = mosaic.find("VRTRasterBand")
    assert band is not None
    assert float(band.findtext("NoDataValue", "")) == NODATA_M
    rects = [source.find("DstRect") for source in band.findall("SimpleSource")]
    offsets = sorted(
        (int(rect.get("xOff", "")), int(rect.get("yOff", "")))
        for rect in rects
        if rect is not None
    )
    assert offsets == [(0, 0), (2 * TILE_PX, 2 * TILE_PX)]

    block = ET.parse(out / "4266-2901.vrt").getroot()  # noqa: S314 - our own file
    raw = block.find("VRTRasterBand")
    assert raw is not None
    assert raw.get("subClass") == "VRTRawRasterBand"
    assert raw.findtext("SourceFilename") == "4266-2901.f32"
    assert raw.findtext("ByteOrder") == "LSB"
    assert raw.findtext("LineOffset") == str(2 * TILE_PX * 4)
    block_gt = [float(v) for v in block.findtext("GeoTransform", "").split(",")]
    assert block_gt[0] == pytest.approx(-MERCATOR_HALF + 8532 * size)
