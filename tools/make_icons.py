"""Generate the PWA icons with the standard library only (no Pillow).

Draws the same mark the site header uses — a navy field, a white envelope and a
notification dot — at the sizes a PWA manifest and iOS home screen need.

    python tools/make_icons.py

Deterministic: running it twice produces byte-identical files, so the icons can
be regenerated and diffed like any other source asset.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "pilot_app" / "static"
NAVY = (18, 59, 99)          # --navy, matches theme_color
BLUE = (23, 105, 170)        # --blue
WHITE = (255, 255, 255)
ALERT = (180, 35, 24)        # --bad, the "you have mail" dot

SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}


def write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    height = len(pixels)
    width = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None)
        for red, green, blue in row:
            raw += bytes((red, green, blue))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def rounded(size: int, radius_ratio: float = 0.22) -> list[list[tuple[int, int, int]]]:
    """Navy rounded square, a white envelope and a red unread dot."""
    # Work at 4x and downsample: gives clean antialiased edges without Pillow.
    scale = 4
    big = size * scale
    canvas = [[(0, 0, 0)] * big for _ in range(big)]

    # Rounded-square mask with a top-to-bottom navy -> blue gradient.
    radius = big * radius_ratio
    for y in range(big):
        for x in range(big):
            if _inside_rounded(x + 0.5, y + 0.5, big, radius):
                blend = y / max(big - 1, 1)
                canvas[y][x] = tuple(round(NAVY[i] + (BLUE[i] - NAVY[i]) * blend) for i in range(3))

    # Envelope: body, then the flap drawn strictly inside it (inset and with a
    # lower apex, so the strokes never poke outside the white rectangle).
    left, right = big * 0.20, big * 0.80
    top, bottom = big * 0.30, big * 0.70
    _rect(canvas, left, top, right, bottom, WHITE)
    inset = big * 0.070
    apex_y = big * 0.615
    _line(canvas, left + inset, top + inset, big * 0.50, apex_y, big * 0.026, NAVY)
    _line(canvas, right - inset, top + inset, big * 0.50, apex_y, big * 0.026, NAVY)

    # Unread dot, top-right, with a navy ring so it reads on any background.
    dot_cx, dot_cy, dot_r = big * 0.755, big * 0.245, big * 0.100
    _disc(canvas, dot_cx, dot_cy, dot_r + big * 0.026, NAVY)
    _disc(canvas, dot_cx, dot_cy, dot_r, ALERT)

    return _downsample(canvas, scale)


def _downsample(canvas, scale: int) -> list[list[tuple[int, int, int]]]:
    """Box-filter downscale by an integer factor (cheap antialiasing)."""
    big = len(canvas)
    size = big // scale
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            red = green = blue = 0
            for dy in range(scale):
                source = canvas[y * scale + dy]
                for dx in range(scale):
                    pixel = source[x * scale + dx]
                    red += pixel[0]; green += pixel[1]; blue += pixel[2]
            count = scale * scale
            row.append((red // count, green // count, blue // count))
        out.append(row)
    return out


def _rect(canvas, left, top, right, bottom, colour) -> None:
    """Plain filled rectangle, clipped to the canvas."""
    height, width = len(canvas), len(canvas[0])
    for y in range(max(0, int(top)), min(height, int(bottom) + 1)):
        for x in range(max(0, int(left)), min(width, int(right) + 1)):
            canvas[y][x] = colour


def _inside_rounded(x: float, y: float, size: float, radius: float) -> bool:
    if radius <= x < size - radius or radius <= y < size - radius:
        return 0 <= x < size and 0 <= y < size
    corner_x = radius if x < radius else size - radius - 1
    corner_y = radius if y < radius else size - radius - 1
    return (x - corner_x) ** 2 + (y - corner_y) ** 2 <= radius ** 2


def _fill_rounded_rect(canvas, left, top, right, bottom, radius, colour) -> None:
    for y in range(int(top), int(bottom) + 1):
        for x in range(int(left), int(right) + 1):
            inside = True
            for cx, cy in ((left + radius, top + radius), (right - radius, top + radius),
                           (left + radius, bottom - radius), (right - radius, bottom - radius)):
                if ((x < left + radius and y < top + radius) or
                        (x > right - radius and y < top + radius) or
                        (x < left + radius and y > bottom - radius) or
                        (x > right - radius and y > bottom - radius)):
                    if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                        inside = False
            if inside and 0 <= y < len(canvas) and 0 <= x < len(canvas[0]):
                canvas[y][x] = colour


def _line(canvas, x0, y0, x1, y1, width, colour) -> None:
    steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
    for step in range(steps + 1):
        t = step / steps
        _disc(canvas, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, width / 2, colour)


def _disc(canvas, cx, cy, radius, colour) -> None:
    size = len(canvas)
    for y in range(max(0, int(cy - radius)), min(size, int(cy + radius) + 2)):
        for x in range(max(0, int(cx - radius)), min(size, int(cx + radius) + 2)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                canvas[y][x] = colour


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        target = OUT_DIR / name
        write_png(target, rounded(size))
        print(f"{target.name}: {size}x{size}, {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
