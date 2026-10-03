"""Tests for the terrain-class pixel contract and its kernel (SNOW-987).

Two things are pinned here. The encoding, because the browser decodes these
pixels in another repository and a byte that moves is a slope that changes
colour without anyone noticing. And the kernel, because the whole point of the
tileset is that a pixel says what ``sample_slope`` says — so ``horn`` is checked
against the literal answers snowdesk-data-pipeline's own tests pin for
``_horn``, not merely against itself.
"""

from __future__ import annotations

import io
import json
import math
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from terrain_class import (  # noqa: E402
    ALPHA,
    BAND_COUNT,
    KERNEL_OFFSETS,
    LEVEL,
    MAX_BAND,
    MAX_ZOOM,
    MIN_ZOOM,
    NO_DATA,
    NODATA_CLASS,
    NODATA_PIXEL,
    OCTANTS,
    TILE_PIXELS,
    WINDOW_SPACING_M,
    WINDOW_STEP_CELLS,
    TerrainClass,
    band_for,
    class_byte,
    classify_kernel,
    decode_class_byte,
    decode_pixel,
    describe,
    descriptor,
    encode_pixel,
    expected_class,
    height_metres,
    horn,
    kernel_at,
    locate,
    main,
    mercator_extent,
    octant_index,
    pixel_lonlat,
    read_pixel,
    world_pixel,
)
from terrain_grid import (  # noqa: E402
    NODATA,
    STORED_CELLS,
    TILE_BYTES,
    cell_for,
    lonlat_to_grid,
    tile_bounds,
)

# The nine stored values of the 10 m kernel at Zermatt village (7.7491 E,
# 46.0207 N) in the real tile 3239/1990 that snowdesk-data-pipeline commits as
# a fixture, row-major from the north-west. Read out of that tile through this
# repo's own addressing; the data pipeline's test_terrain.py pins
# sample_slope's answer on them at window_m=10 as 8.111279 degrees facing
# 105.255119, and those two literals are asserted below.
ZERMATT_KERNEL = [6412, 6412, 6408, 6412, 6411, 6397, 6410, 6411, 6400]
ZERMATT_ANGLE = 8.111279
ZERMATT_ASPECT = 105.255119


def plane(east_gradient: float = 0.0, south_gradient: float = 0.0) -> list[float]:
    """Return the nine heights of a plane, as the data pipeline's tests build."""
    return [
        1000.0
        + col * WINDOW_SPACING_M * east_gradient
        + row * WINDOW_SPACING_M * south_gradient
        for row in (-1, 0, 1)
        for col in (-1, 0, 1)
    ]


# --- The kernel, against sample_slope's own answers --------------------------


def test_the_window_is_sample_slopes_default() -> None:
    # 10 m over a 5 m grid: cells two apart. A step of 1 would be a 5 m window
    # and a different answer on every slope that is not a plane.
    assert WINDOW_STEP_CELLS == 2
    assert WINDOW_SPACING_M == 10
    assert KERNEL_OFFSETS[0] == (-2, -2)
    assert KERNEL_OFFSETS[4] == (0, 0)
    assert KERNEL_OFFSETS[8] == (2, 2)


def test_horn_on_the_real_zermatt_kernel() -> None:
    angle, aspect = horn([value * 0.25 for value in ZERMATT_KERNEL], 10)

    assert angle == pytest.approx(ZERMATT_ANGLE, abs=1e-6)
    assert aspect == pytest.approx(ZERMATT_ASPECT, abs=1e-6)


def test_the_zermatt_pixel() -> None:
    # 6411 quarter-metres is 1602.75 m, which rounds half up to 1603. 8.1
    # degrees is band 1 (5-10), and 105 degrees faces east.
    assert classify_kernel(ZERMATT_KERNEL) == (6, 67, 2 << 5 | 1, 255)
    assert describe(decode_pixel(6, 67, 65, 255)) == "1603 m, band 5-10, E"


def test_flat_ground_is_zero_and_faces_nowhere() -> None:
    assert horn(plane(), 10) == (0.0, None)


@pytest.mark.parametrize(
    ("east", "south", "angle", "aspect"),
    [
        # The planes snowdesk-data-pipeline's TestSlopeOnAKnownPlane uses.
        (1.0, 0.0, 45.0, 270.0),
        (-1.0, 0.0, 45.0, 90.0),
        (0.0, 1.0, 45.0, 0.0),
        (0.0, -1.0, 45.0, 180.0),
        (-1.0, -1.0, math.degrees(math.atan(math.sqrt(2))), 135.0),
        (-0.25, 0.0, math.degrees(math.atan(0.25)), 90.0),
    ],
)
def test_horn_on_known_planes(
    east: float, south: float, angle: float, aspect: float
) -> None:
    got_angle, got_aspect = horn(plane(east, south), 10)

    assert got_angle == pytest.approx(angle)
    assert got_aspect == pytest.approx(aspect)


def test_aspect_is_the_direction_of_descent() -> None:
    # Rising eastward falls westward. A mirrored atan2 still gives an answer in
    # range, so the direction is worth one assertion of its own.
    _, aspect = horn(plane(east_gradient=0.5), 10)

    assert aspect is not None
    assert OCTANTS[octant_index(aspect) or 0] == "W"


# --- Octants ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("bearing", "expected"),
    [
        (0.0, "N"),
        (10.0, "N"),
        (350.0, "N"),
        (45.0, "NE"),
        (90.0, "E"),
        (180.0, "S"),
        (270.0, "W"),
        (315.0, "NW"),
        # The edges: each octant owns its anticlockwise edge.
        (337.4999, "NW"),
        (337.5, "N"),
        (22.4999, "N"),
        (22.5, "NE"),
        (360.0, "N"),
        (-0.0001, "N"),
    ],
)
def test_octant_edges(bearing: float, expected: str) -> None:
    # The first eight are snowdesk-data-pipeline's own TestOctantFor cases.
    assert OCTANTS[octant_index(bearing) or 0] == expected
    assert octant_index(bearing) == OCTANTS.index(expected)


def test_no_bearing_is_no_octant() -> None:
    assert octant_index(None) is None


# --- Bands -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle", "band"),
    [
        (0.0, 0),
        (4.999, 0),
        (5.0, 1),
        (29.999, 5),
        (30.0, 6),
        (35.0, 7),
        (40.0, 8),
        (84.999, 16),
        (85.0, 17),
        (89.99, 17),
        (90.0, 17),
    ],
)
def test_band_edges(angle: float, band: int) -> None:
    assert band_for(angle) == band


def test_a_negative_angle_is_a_bug() -> None:
    with pytest.raises(ValueError, match="negative"):
        band_for(-1.0)


def test_level_ground_gets_its_own_code() -> None:
    assert class_byte(0.0, None) == LEVEL


def test_no_real_class_reaches_the_reserved_codes() -> None:
    assert class_byte(89.9, 337.0) == 7 << 5 | 17 == 241
    assert max(class_byte(a, b) for a in (0.0, 89.9) for b in (0.0, 337.0)) < LEVEL


# --- Encoding --------------------------------------------------------------


ALL_CLASS_BYTES = [
    octant << 5 | band for octant in range(len(OCTANTS)) for band in range(BAND_COUNT)
] + [LEVEL, NO_DATA]


@pytest.mark.parametrize("b", ALL_CLASS_BYTES)
def test_every_class_round_trips(b: int) -> None:
    pixel = encode_pixel(4321, b)
    decoded = decode_pixel(*pixel)

    if b == NO_DATA:
        assert pixel == NODATA_PIXEL
        assert decoded == NODATA_CLASS
        assert decoded.is_nodata
    elif b == LEVEL:
        assert decoded == TerrainClass(4321, 0, None)
        assert decoded.is_level
    else:
        assert decoded == TerrainClass(4321, b & 31, OCTANTS[b >> 5])
        assert not decoded.is_level
    assert pixel[3] == ALPHA


def test_there_are_146_codes() -> None:
    assert len(set(ALL_CLASS_BYTES)) == 8 * 18 + 2


def test_height_is_big_endian() -> None:
    assert encode_pixel(0x1234, 0)[:2] == (0x12, 0x34)
    assert decode_pixel(0x12, 0x34, 0, 255).height_m == 0x1234


def test_heights_round_half_up() -> None:
    # Not banker's rounding: 1602.5 and 1603.5 both go up.
    assert height_metres(6410) == 1603  # 1602.5
    assert height_metres(6414) == 1604  # 1603.5
    assert height_metres(6409) == 1602  # 1602.25
    assert height_metres(6411) == 1603  # 1602.75


def test_a_negative_height_does_not_wrap() -> None:
    with pytest.raises(ValueError, match="16 unsigned bits"):
        encode_pixel(-1, 0)


@pytest.mark.parametrize("b", [18, 31, 242, 253])
def test_a_byte_no_encoder_writes_is_refused(b: int) -> None:
    with pytest.raises(ValueError, match="not a valid class"):
        decode_class_byte(b)


def test_a_translucent_pixel_is_refused() -> None:
    # A canvas premultiplies; anything below 255 alpha has had its data rounded.
    with pytest.raises(ValueError, match="alpha"):
        decode_pixel(6, 67, 65, 254)


def test_a_nodata_pixel_carrying_a_height_is_refused() -> None:
    with pytest.raises(ValueError, match="carries a height"):
        decode_pixel(6, 67, NO_DATA, 255)


# --- The scalar kernel over stored values -----------------------------------


def test_one_hole_voids_the_kernel() -> None:
    for index in range(9):
        kernel = list(ZERMATT_KERNEL)
        kernel[index] = NODATA
        assert classify_kernel(kernel) == NODATA_PIXEL


def test_a_flat_kernel_is_level_with_a_height() -> None:
    assert classify_kernel([6000] * 9) == (5, 220, LEVEL, 255)


def test_a_kernel_must_have_nine_cells() -> None:
    with pytest.raises(ValueError, match="9"):
        classify_kernel([0] * 8)


# --- Web Mercator ------------------------------------------------------------


def test_world_pixel_round_trips() -> None:
    lon, lat = pixel_lonlat(*world_pixel(7.7491, 46.0207, 14), 14)

    assert lon == pytest.approx(7.7491, abs=1e-9)
    assert lat == pytest.approx(46.0207, abs=1e-9)


def test_locate_zermatt() -> None:
    # The standard XYZ tile for Zermatt at z14 — checkable on any slippy map.
    zoom, tile_x, tile_y, px, py = locate(7.7491, 46.0207, 14)

    assert (zoom, tile_x, tile_y) == (14, 8544, 5827)
    assert 0 <= px < TILE_PIXELS
    assert 0 <= py < TILE_PIXELS


def test_mercator_extent_is_tile_aligned_with_a_margin() -> None:
    extent = mercator_extent(7.70, 45.98, 7.80, 46.05, 14)
    tile_m = 2 * math.pi * 6378137.0 / 2**14

    assert extent.tile_x == locate(7.70, 46.05, 14)[1] - 1
    assert extent.tile_y == locate(7.70, 46.05, 14)[2] - 1
    assert extent.tile_x + extent.tiles_x - 1 == locate(7.80, 45.98, 14)[1] + 1
    assert extent.width == extent.tiles_x * 256
    assert (extent.east - extent.west) == pytest.approx(extent.tiles_x * tile_m)
    assert (extent.north - extent.south) == pytest.approx(extent.tiles_y * tile_m)


def test_mercator_extent_stays_inside_the_world() -> None:
    extent = mercator_extent(-180.0, -85.0, 180.0, 85.0, 1)

    assert (extent.tile_x, extent.tile_y, extent.tiles_x, extent.tiles_y) == (
        0,
        0,
        2,
        2,
    )


# --- The descriptor ----------------------------------------------------------


def test_descriptor_states_the_contract() -> None:
    doc = descriptor("https://example.test/", "v1")

    assert doc["tiles"] == ["https://example.test/terrain-class/v1/{z}/{x}/{y}.png"]
    assert (doc["minzoom"], doc["maxzoom"]) == (MIN_ZOOM, MAX_ZOOM) == (12, 14)
    assert doc["encoding"]["octants"] == list(OCTANTS)
    assert doc["encoding"]["level"] == 254
    assert doc["encoding"]["nodata"] == 255
    assert doc["encoding"]["analysis_window_m"] == 10
    assert doc["encoding"]["bands"] == MAX_BAND + 1
    assert "level" in doc["absent_tile"]


def test_descriptor_without_an_origin_has_no_urls() -> None:
    assert "tiles" not in descriptor()


def test_descriptor_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["descriptor", "--origin", "https://o.test", "--version", "v2"]) == 0

    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "v2"


def test_pixel_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pixel", "1603", "8.11", "105.25"]) == 0
    assert capsys.readouterr().out.split() == ["6", "67", "65", "255"]

    assert main(["pixel", "1603", "0", "none"]) == 0
    assert capsys.readouterr().out.split() == ["6", "67", "254", "255"]


def test_mercator_extent_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"bbox": [7.70, 45.98, 7.80, 46.05]}))

    assert main(["mercator-extent", "--manifest", str(manifest), "--zoom", "13"]) == 0
    fields = capsys.readouterr().out.split()
    extent = mercator_extent(7.70, 45.98, 7.80, 46.05, 13)
    assert float(fields[0]) == extent.west
    assert [int(value) for value in fields[4:]] == [
        extent.width,
        extent.height,
        extent.tile_x,
        extent.tile_y,
        extent.tiles_x,
        extent.tiles_y,
    ]


# --- Reading a published tile ------------------------------------------------


def png(rows: list[bytes], filter_type: int = 0) -> bytes:
    """Return a minimal RGBA PNG, every row with one filter type."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    width = len(rows[0]) // 4
    header = struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0)
    raw = b"".join(bytes([filter_type]) + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_read_pixel_from_a_png() -> None:
    rows = [bytes(NODATA_PIXEL) * 2, bytes((6, 67, 65, 255)) + bytes(NODATA_PIXEL)]

    assert read_pixel(png(rows), 0, 1) == TerrainClass(1603, 1, "E")
    assert read_pixel(png(rows), 1, 1) == NODATA_CLASS


def test_read_pixel_undoes_the_up_filter() -> None:
    # A row filtered "Up" stores its difference from the row above.
    first = bytes((6, 60, 65, 255))
    second = bytes((0, 7, 0, 0))

    assert read_pixel(png([first, second], filter_type=2), 0, 1) == TerrainClass(
        1603, 1, "E"
    )


def test_read_pixel_refuses_rgb() -> None:
    body = png([bytes(4)]).replace(
        struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0),
        struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0),
    )

    with pytest.raises(ValueError, match="RGBA"):
        read_pixel(body, 0, 0)


def test_read_pixel_cli_reports_a_broken_tile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stdin:
        buffer = io.BytesIO(b"not a png")

    monkeypatch.setattr(sys, "stdin", Stdin)

    assert main(["read-pixel", "0", "0"]) == 1
    assert capsys.readouterr().out.startswith("broken:")


# --- The expected answer, from the elevation tiles --------------------------


def fake_terrain(fill: int | None) -> dict[tuple[int, int], bytes]:
    """Return elevation tiles around Zermatt holding one constant value."""
    value = NODATA if fill is None else fill
    body = struct.pack("<h", value) * (STORED_CELLS * STORED_CELLS)
    assert len(body) == TILE_BYTES
    cell = cell_for(*lonlat_to_grid(7.7491, 46.0207))
    return {
        (cell.tile_x + dx, cell.tile_y + dy): body
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    }


def test_expected_class_on_flat_ground() -> None:
    tiles = fake_terrain(6000)

    got = expected_class(7.7491, 46.0207, 14, lambda x, y: tiles.get((x, y)))

    assert got == "1500 m, band 0-5, level"


def test_expected_class_outside_coverage_is_nodata() -> None:
    assert expected_class(7.7491, 46.0207, 14, lambda x, y: None) == "nodata"


def test_a_kernel_at_a_tile_corner_reads_its_neighbours() -> None:
    # The kernel reaches two cells and the skirt is one, so a kernel on a tile's
    # outermost data cell has to read the neighbouring tiles themselves. Each
    # tile here holds its own constant, so the value says which tile it came
    # from.
    tile_x, tile_y = 3239, 1990
    tiles = {
        (tile_x + dx, tile_y + dy): struct.pack("<h", 1000 + 10 * dx + dy)
        * (STORED_CELLS * STORED_CELLS)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    }
    calls: list[tuple[int, int]] = []

    def fetch(x: int, y: int) -> bytes | None:
        calls.append((x, y))
        return tiles.get((x, y))

    # The centre of the tile's north-west data cell.
    west, _, _, north = tile_bounds(tile_x, tile_y)
    kernel = kernel_at(west + 2.5, north - 2.5, fetch)

    # North is +y, west is -x.
    assert kernel[0] == 1000 - 10 + 1  # north-west tile
    assert kernel[1] == 1000 + 1  # north
    assert kernel[3] == 1000 - 10  # west
    assert kernel[4] == 1000  # the tile itself
    assert kernel[8] == 1000  # two cells south-east is still inside it
    assert sorted(set(calls)) == sorted(
        {
            (tile_x - 1, tile_y + 1),
            (tile_x, tile_y + 1),
            (tile_x - 1, tile_y),
            (tile_x, tile_y),
        }
    )
    assert len(calls) == 4  # each tile fetched once
