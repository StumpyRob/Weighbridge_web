from __future__ import annotations

import base64
import binascii
import zlib

SIGNATURE_DATA_URL_PREFIX = "data:image/png;base64,"
SIGNATURE_MAX_BYTES = 500_000


def normalize_png_data_url(value: str | None) -> tuple[str, bytes] | None:
    raw_value = str(value or "").strip()
    if not raw_value or not raw_value.startswith(SIGNATURE_DATA_URL_PREFIX):
        return None
    encoded = raw_value[len(SIGNATURE_DATA_URL_PREFIX) :].strip()
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if len(decoded) > SIGNATURE_MAX_BYTES:
        return None
    return f"{SIGNATURE_DATA_URL_PREFIX}{encoded}", decoded


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_png_row(
    row: bytearray,
    *,
    prev_row: bytearray | None,
    filter_type: int,
    bytes_per_pixel: int,
) -> bool:
    row_len = len(row)
    if filter_type == 0:
        return True
    if filter_type == 1:
        for idx in range(row_len):
            left = row[idx - bytes_per_pixel] if idx >= bytes_per_pixel else 0
            row[idx] = (row[idx] + left) & 0xFF
        return True
    if filter_type == 2:
        for idx in range(row_len):
            up = prev_row[idx] if prev_row is not None else 0
            row[idx] = (row[idx] + up) & 0xFF
        return True
    if filter_type == 3:
        for idx in range(row_len):
            left = row[idx - bytes_per_pixel] if idx >= bytes_per_pixel else 0
            up = prev_row[idx] if prev_row is not None else 0
            row[idx] = (row[idx] + ((left + up) // 2)) & 0xFF
        return True
    if filter_type == 4:
        for idx in range(row_len):
            left = row[idx - bytes_per_pixel] if idx >= bytes_per_pixel else 0
            up = prev_row[idx] if prev_row is not None else 0
            up_left = (
                prev_row[idx - bytes_per_pixel]
                if prev_row is not None and idx >= bytes_per_pixel
                else 0
            )
            row[idx] = (row[idx] + _paeth_predictor(left, up, up_left)) & 0xFF
        return True
    return False


def png_has_visible_ink(png_bytes: bytes) -> bool:
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    cursor = 8
    width = 0
    height = 0
    color_type = -1
    saw_ihdr = False
    compressed_parts: list[bytes] = []

    while cursor + 8 <= len(png_bytes):
        chunk_length = int.from_bytes(png_bytes[cursor : cursor + 4], "big")
        chunk_type = png_bytes[cursor + 4 : cursor + 8]
        cursor += 8
        if cursor + chunk_length + 4 > len(png_bytes):
            return False
        chunk_data = png_bytes[cursor : cursor + chunk_length]
        cursor += chunk_length
        cursor += 4

        if chunk_type == b"IHDR":
            if chunk_length != 13:
                return False
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth = int(chunk_data[8])
            color_type = int(chunk_data[9])
            compression = int(chunk_data[10])
            filter_method = int(chunk_data[11])
            interlace = int(chunk_data[12])
            if (
                width <= 0
                or height <= 0
                or bit_depth != 8
                or color_type not in (2, 6)
                or compression != 0
                or filter_method != 0
                or interlace != 0
            ):
                return False
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            compressed_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not saw_ihdr or not compressed_parts:
        return False
    try:
        decompressed = zlib.decompress(b"".join(compressed_parts))
    except zlib.error:
        return False

    channels = 3 if color_type == 2 else 4
    row_bytes = width * channels
    expected_length = (row_bytes + 1) * height
    if len(decompressed) != expected_length:
        return False

    previous_row: bytearray | None = None
    offset = 0
    for _ in range(height):
        filter_type = int(decompressed[offset])
        offset += 1
        row = bytearray(decompressed[offset : offset + row_bytes])
        offset += row_bytes
        if len(row) != row_bytes:
            return False
        if not _unfilter_png_row(
            row,
            prev_row=previous_row,
            filter_type=filter_type,
            bytes_per_pixel=channels,
        ):
            return False

        if color_type == 2:
            for idx in range(0, row_bytes, 3):
                red = row[idx]
                green = row[idx + 1]
                blue = row[idx + 2]
                if red < 250 or green < 250 or blue < 250:
                    return True
        else:
            for idx in range(0, row_bytes, 4):
                red = row[idx]
                green = row[idx + 1]
                blue = row[idx + 2]
                alpha = row[idx + 3]
                if alpha == 0:
                    continue
                if alpha < 250 or red < 250 or green < 250 or blue < 250:
                    return True
        previous_row = row

    return False
