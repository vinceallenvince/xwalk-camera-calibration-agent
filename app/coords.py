"""Coordinate-space conversion.

Gemini reports spatial output in a normalized 0-1000 grid, not source pixels.
Rather than fight that prior with prompt wording, the agent speaks 0-1000 in
both directions — the reference is handed over normalized, the response comes
back normalized — and conversion to pixels happens here, deterministically.
"""

import struct

NORM = 1000.0


def sniff_image_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) for PNG or JPEG bytes, without pulling in Pillow."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)

    if data[:2] == b"\xff\xd8":
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            # SOF0..SOF15, excluding DHT/JPG/DAC markers.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return int(width), int(height)
            (segment_length,) = struct.unpack(">H", data[index + 2 : index + 4])
            index += 2 + segment_length
    raise ValueError("Unsupported image format; expected PNG or JPEG")


def to_normalized(polygon, width: int, height: int) -> list[list[float]]:
    return [[round(x / width * NORM, 1), round(y / height * NORM, 1)] for x, y in polygon]


def to_pixels(polygon, width: int, height: int) -> list[list[float]]:
    return [[round(x / NORM * width, 2), round(y / NORM * height, 2)] for x, y in polygon]
