"""Generate the interface background images with the standard library only.

No photo assets and therefore no attribution obligation: every background is
painted here from a small deterministic description (gradient + bloom +
skyline/hills/water/rules) and written as a PNG. Deterministic means the same
command always produces the same bytes, so the images can live in the repo like
any other source asset, and we can re-tune them without hunting for a licence.

Rendering happens at a quarter of the final size and is then interpolated up.
That is not a shortcut for speed alone: a smooth ramp at full resolution shows
banding and compresses badly, while the interpolated version is continuous and
an order of magnitude smaller. Soft (slightly blurred) edges are also what
makes a background read as a considered piece of art direction rather than a
sharp, obviously machine-made graphic.

    python tools/make_backgrounds.py            # writes pilot_app/static/bg-*.png
    python tools/make_backgrounds.py --out DIR  # somewhere else (design mockups)
    python tools/make_backgrounds.py --only bg-dusk.png --scale 1.0
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

RGB = tuple[int, int, int]
Pixels = list[list[RGB]]

FINAL_WIDTH, FINAL_HEIGHT = 1280, 720
DIVISOR = 4  # compute at 1/4 resolution, then bilinear-upscale


# --------------------------------------------------------------------------- #
# small deterministic helpers (no random module: identical output everywhere)
# --------------------------------------------------------------------------- #
class Rng:
    """Tiny LCG so a background renders identically on every machine."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    def next(self) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state / 0x7FFFFFFF


def mix(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def over(base: RGB, top: RGB, alpha: float) -> RGB:
    return mix(base, top, alpha)


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def ramp(stops: tuple[tuple[float, RGB], ...], t: float) -> RGB:
    """Sample a vertical colour ramp defined at positions 0..1."""
    t = max(0.0, min(1.0, t))
    for index in range(len(stops) - 1):
        p0, c0 = stops[index]
        p1, c1 = stops[index + 1]
        if t <= p1 or index == len(stops) - 2:
            span = p1 - p0
            return mix(c0, c1, 0.0 if span <= 0 else (t - p0) / span)
    return stops[-1][1]


def glow(nx: float, ny: float, cx: float, cy: float, radius: float) -> float:
    """Soft radial falloff in 0..1, used for lights, suns and vignettes."""
    dx = (nx - cx) * 1.6  # pages are wider than tall: stretch x so it stays round
    dy = ny - cy
    d2 = (dx * dx + dy * dy) / (radius * radius)
    return math.exp(-d2 * 1.6)


def vignette(nx: float, ny: float, strength: float, power: float = 2.0) -> float:
    dx = (nx - 0.5) * 2.0
    dy = (ny - 0.5) * 2.0
    d = min(1.0, math.hypot(dx * 0.62, dy))
    return 1.0 - strength * (d ** power)


# --------------------------------------------------------------------------- #
# scene painters
# --------------------------------------------------------------------------- #
def paint_paper(width: int, height: int) -> Pixels:
    """Warm laid-paper sheet: faint horizontal ruling, edge shading, light bloom."""
    rows: Pixels = []
    for y in range(height):
        ny = y / (height - 1)
        row: list[RGB] = []
        for x in range(width):
            nx = x / (width - 1)
            base = ramp(((0.0, (252, 251, 248)), (0.45, (247, 245, 240)), (1.0, (241, 239, 234))), ny)
            base = over(base, (233, 236, 242), 0.55 * glow(nx, ny, 0.03, 0.02, 0.85))
            base = over(base, (250, 243, 230), 0.45 * glow(nx, ny, 0.92, 0.10, 0.75))
            pixel = mix(base, (228, 224, 215), 0.38 * vignette(nx, ny, 1.0, 2.4))

            # two pixel-high rulings every ~34 final pixels: reads as a pad of paper
            rule = (y * DIVISOR) % 34
            if rule < 2:
                pixel = over(pixel, (214, 210, 200), 0.30)
            # a single vertical margin rule, like a printed ledger
            if abs(x * DIVISOR - 148) < 2:
                pixel = over(pixel, (214, 200, 190), 0.18)
            row.append(pixel)
        rows.append(row)
    return rows


def paint_dusk(width: int, height: int) -> Pixels:
    """Night sky over a low skyline with a scatter of lit windows."""
    rng = Rng(20260913)
    stars = [(rng.next(), rng.next() * 0.55, 0.35 + rng.next() * 0.65) for _ in range(150)]

    # skyline: two rows of blocks so the silhouette is not a single flat bar
    def skyline(count: int, top: float, seed: int, jitter: float) -> list[float]:
        local = Rng(seed)
        heights = []
        for _ in range(count):
            heights.append(top + (local.next() - 0.5) * jitter)
        return heights

    back = skyline(46, 0.640, 7717, 0.10)
    front = skyline(31, 0.745, 991, 0.13)
    windows: list[tuple[float, float]] = []
    local = Rng(4242)
    for index in range(150):
        block = index % len(front)
        left = block / len(front)
        windows.append((left + local.next() * (1.0 / len(front)) * 0.7,
                        front[block] + 0.02 + local.next() * (1.0 - front[block] - 0.05)))

    rows: Pixels = []
    for y in range(height):
        ny = y / (height - 1)
        row: list[RGB] = []
        for x in range(width):
            nx = x / (width - 1)
            pixel = ramp(((0.0, (14, 24, 44)), (0.42, (11, 19, 36)), (0.70, (10, 16, 30)), (1.0, (7, 11, 21))), ny)
            pixel = over(pixel, (46, 84, 136), 0.85 * glow(nx, ny, 0.18, 0.06, 0.72))
            pixel = over(pixel, (24, 40, 70), 0.55 * glow(nx, ny, 0.86, 0.30, 0.60))
            # moon: soft halo plus a disc that fades out instead of ending on a
            # hard threshold (a thresholded disc upscales into a visible hexagon)
            pixel = over(pixel, (104, 138, 186), 0.34 * glow(nx, ny, 0.80, 0.155, 0.26))
            pixel = over(pixel, (232, 238, 248), min(1.0, 1.35 * glow(nx, ny, 0.80, 0.155, 0.026)))
            # stars, brighter near the top of the sky
            for sx, sy, brightness in stars:
                if abs(nx - sx) * width < 1.0 and abs(ny - sy) * height < 1.0:
                    pixel = over(pixel, (226, 236, 255), 0.75 * brightness)
                    break

            # warm city glow sitting just behind the back row
            if ny > 0.55:
                pixel = over(pixel, (58, 62, 92), 0.35 * smoothstep((ny - 0.55) / 0.25))

            block = min(len(back) - 1, int(nx * len(back)))
            if ny > back[block]:
                pixel = over(pixel, (16, 26, 46), 0.55)
            block = min(len(front) - 1, int(nx * len(front)))
            if ny > front[block]:
                pixel = over(pixel, (6, 10, 18), 0.94)

            pixel = mix(pixel, (4, 7, 14), 0.45 * vignette(nx, ny, 1.0, 2.2))
            row.append(pixel)
        rows.append(row)

    # stamp a few lit windows after the base pass so they stay crisp against the blocks
    for wx, wy in windows:
        cx, cy = int(wx * (width - 1)), int(wy * (height - 1))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                px, py = cx + dx, cy + dy
                if 0 <= px < width and 0 <= py < height:
                    rows[py][px] = over(rows[py][px], (255, 208, 138), 0.55)
    return rows


def paint_harbour(width: int, height: int) -> Pixels:
    """Calm dusk harbour: warm sky, layered hills, sun and its reflection."""
    horizon = 0.560
    sun_x, sun_y = 0.735, 0.330

    def ridge(nx: float, base: float, amp: float, phase: float) -> float:
        return base + amp * (math.sin(nx * 5.1 + phase) * 0.55
                             + math.sin(nx * 2.3 - phase * 0.7) * 0.30
                             + math.sin(nx * 11.0 + phase * 1.9) * 0.15)

    rows: Pixels = []
    for y in range(height):
        ny = y / (height - 1)
        row: list[RGB] = []
        for x in range(width):
            nx = x / (width - 1)
            if ny < horizon:
                pixel = ramp(((0.0, (196, 219, 233)), (0.34, (223, 228, 224)),
                              (0.62, (246, 226, 203)), (1.0, (250, 236, 219))), ny / horizon)
                pixel = over(pixel, (255, 238, 206), 0.70 * glow(nx, ny, sun_x, sun_y, 0.42))
                if glow(nx, ny, sun_x, sun_y, 0.055) > 0.5:
                    pixel = over(pixel, (255, 249, 232), 0.75)
                far = ridge(nx, 0.470, 0.035, 0.4)
                if ny > far:
                    pixel = over(pixel, (215, 208, 202), 0.50)
                near = ridge(nx, 0.520, 0.026, 2.1)
                if ny > near:
                    pixel = over(pixel, (186, 178, 176), 0.62)
            else:
                depth = (ny - horizon) / (1.0 - horizon)
                pixel = ramp(((0.0, (232, 220, 210)), (0.55, (222, 213, 207)), (1.0, (206, 200, 198))), depth)
                # sun column: light spilling down the water, wobbling slightly
                column = glow(nx, 0.5, sun_x, 0.5, 0.10) * (1.0 - depth * 0.65)
                # stretch the column vertically by sampling with a compressed y
                column = glow(nx, ny * 0.25 + horizon * 0.75, sun_x, sun_y + 0.12, 0.075) * (1.0 - depth * 0.5)
                pixel = over(pixel, (255, 240, 214), 0.85 * column)
                streak = (1.0 + math.sin(ny * 190.0 + math.sin(nx * 21.0) * 1.6)) * 0.5
                pixel = over(pixel, (188, 184, 186), 0.16 * streak * (0.35 + depth))
                pixel = over(pixel, (150, 120, 104), 0.30 * smoothstep((ny - horizon) / 0.06))
            pixel = mix(pixel, (196, 190, 188), 0.30 * vignette(nx, ny, 1.0, 2.3))
            row.append(pixel)
        rows.append(row)
    return rows


def paint_night(width: int, height: int) -> Pixels:
    """Near-black minimal field: one soft top light and a quiet ring motif."""
    rng = Rng(31337)
    stars = [(rng.next(), rng.next() * 0.40, 0.25 + rng.next() * 0.75) for _ in range(34)]
    rows: Pixels = []
    for y in range(height):
        ny = y / (height - 1)
        row: list[RGB] = []
        for x in range(width):
            nx = x / (width - 1)
            pixel = ramp(((0.0, (14, 17, 20)), (0.55, (10, 12, 15)), (1.0, (7, 8, 11))), ny)
            # a single restrained light from above; the previous version used two
            # wide glows and the overlap read as a grey smudge across the top half
            pixel = over(pixel, (34, 46, 50), 0.42 * glow(nx, ny, 0.52, -0.10, 0.62))
            # concentric rings anchored off-canvas: a quiet topographic motif
            d = math.hypot((nx - 1.10) * 1.5, ny - 1.18)
            ring = (d * 21.0) % 1.0
            if ring < 0.05:
                pixel = over(pixel, (48, 68, 72), 0.42 * max(0.0, 1.0 - d * 0.55))
            for sx, sy, brightness in stars:
                if abs(nx - sx) * width < 1.0 and abs(ny - sy) * height < 1.0:
                    pixel = over(pixel, (188, 210, 218), 0.42 * brightness)
                    break
            pixel = mix(pixel, (4, 5, 6), 0.50 * vignette(nx, ny, 1.0, 2.1))
            row.append(pixel)
        rows.append(row)
    return rows


PAINTERS = {
    "bg-paper.png": paint_paper,
    "bg-dusk.png": paint_dusk,
    "bg-harbour.png": paint_harbour,
    "bg-night.png": paint_night,
}


# --------------------------------------------------------------------------- #
# raster plumbing
# --------------------------------------------------------------------------- #
def _bilinear(source: Pixels, width: int, height: int) -> Pixels:
    """Smoothly upscale a low-resolution render to the final image size."""
    src_h = len(source)
    src_w = len(source[0])
    rows: Pixels = []
    for y in range(height):
        sy = (y / max(height - 1, 1)) * (src_h - 1)
        y0, y1 = int(sy), min(int(sy) + 1, src_h - 1)
        fy = sy - y0
        row: list[RGB] = []
        for x in range(width):
            sx = (x / max(width - 1, 1)) * (src_w - 1)
            x0, x1 = int(sx), min(int(sx) + 1, src_w - 1)
            fx = sx - x0
            top, top_right = source[y0][x0], source[y0][x1]
            bottom, bottom_right = source[y1][x0], source[y1][x1]
            value = []
            for channel in range(3):
                upper = top[channel] + (top_right[channel] - top[channel]) * fx
                lower = bottom[channel] + (bottom_right[channel] - bottom[channel]) * fx
                value.append(round(upper + (lower - upper) * fy))
            row.append(tuple(value))  # type: ignore[arg-type]
        rows.append(row)
    return rows


def render(painter, width: int, height: int) -> Pixels:
    small_w = max(2, width // DIVISOR)
    small_h = max(2, height // DIVISOR)
    return _bilinear(painter(small_w, small_h), width, height)


def _filtered_rows(pixels: Pixels) -> bytearray:
    """Serialise rows applying the PNG filter that compresses best.

    Writing every row with filter type 0 (None) is what made these images large:
    a smooth gradient has neighbouring bytes that differ by one or two, which is
    exactly what the Sub/Up filters flatten to zero. Picking the cheapest of the
    three per row (the standard minimum-sum-of-absolute-differences heuristic)
    costs nothing and shrinks the file several times over.
    """
    raw = bytearray()
    previous = bytes(len(pixels[0]) * 3)
    for row in pixels:
        line = bytearray()
        for red, green, blue in row:
            line += bytes((red, green, blue))
        candidates: list[tuple[int, bytes]] = []
        # 0: none
        candidates.append((0, bytes(line)))
        # 1: Sub (difference from the pixel to the left)
        sub = bytearray(len(line))
        for i, value in enumerate(line):
            sub[i] = (value - (line[i - 3] if i >= 3 else 0)) & 0xFF
        candidates.append((1, bytes(sub)))
        # 2: Up (difference from the pixel above)
        up = bytearray(len(line))
        for i, value in enumerate(line):
            up[i] = (value - previous[i]) & 0xFF
        candidates.append((2, bytes(up)))

        def cost(payload: bytes) -> int:
            return sum(value if value < 128 else 256 - value for value in payload)

        filter_type, payload = min(candidates, key=lambda item: cost(item[1]))
        raw.append(filter_type)
        raw += payload
        previous = bytes(line)
    return raw


def write_png(path: Path, pixels: Pixels) -> None:
    height = len(pixels)
    width = len(pixels[0])
    raw = _filtered_rows(pixels)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "pilot_app" / "static"))
    parser.add_argument("--scale", type=float, default=1.0, help="分辨率倍率（预览用 0.5 可加快）")
    parser.add_argument("--only", default=None, help="只生成其中一个文件")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, painter in PAINTERS.items():
        if args.only and args.only != name:
            continue
        width = int(FINAL_WIDTH * args.scale)
        height = int(FINAL_HEIGHT * args.scale)
        target = out_dir / name
        write_png(target, render(painter, width, height))
        print(f"{target}  {width}x{height}  {target.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
