# Файл: make_icon.py
# Генерация assets/icon.ico — зелёная стрелка вниз из контура облака.
# Важно: BMP-формат внутри ICO (не PNG), чтобы Windows и PyInstaller
# стабильно показывали иконку на exe, рабочем столе и панели задач.
from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUT_ICO = ROOT / "assets" / "icon.ico"
OUT_PREVIEW = ROOT / "assets" / "icon_preview.png"

GREEN = (45, 200, 120, 255)
GREEN_HI = (80, 230, 150, 255)
CLOUD = (230, 235, 245, 255)


def draw_icon(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Мелкие размеры: горизонтальная черта + стрелка
    if s <= 20:
        d.rectangle([s * 0.18, s * 0.18, s * 0.82, s * 0.32], fill=CLOUD)
        d.rectangle([s * 0.40, s * 0.32, s * 0.60, s * 0.58], fill=GREEN)
        d.polygon(
            [(s * 0.22, s * 0.52), (s * 0.78, s * 0.52), (s * 0.5, s * 0.90)],
            fill=GREEN,
        )
        return img

    stroke = max(2, s // 14)
    parts = [
        [s * 0.12, s * 0.18, s * 0.48, s * 0.52],
        [s * 0.52, s * 0.18, s * 0.88, s * 0.52],
        [s * 0.28, s * 0.08, s * 0.72, s * 0.48],
    ]
    body = [s * 0.14, s * 0.28, s * 0.86, s * 0.55]

    fill_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fill_layer)
    for b in parts:
        fd.ellipse(b, fill=CLOUD)
    fd.rounded_rectangle(body, radius=max(2, s // 8), fill=CLOUD)

    hole = Image.new("L", (s, s), 0)
    hd = ImageDraw.Draw(hole)
    shrink = stroke
    for b in parts:
        x0, y0, x1, y1 = b
        if x1 - shrink > x0 + shrink and y1 - shrink > y0 + shrink:
            hd.ellipse([x0 + shrink, y0 + shrink, x1 - shrink, y1 - shrink], fill=255)
    hd.rounded_rectangle(
        [body[0] + shrink, body[1] + shrink, body[2] - shrink, body[3] - shrink],
        radius=max(1, s // 8 - shrink // 2),
        fill=255,
    )
    px = fill_layer.load()
    hp = hole.load()
    for y in range(s):
        for x in range(s):
            if hp[x, y] == 255:
                px[x, y] = (0, 0, 0, 0)

    img = Image.alpha_composite(img, fill_layer)
    d = ImageDraw.Draw(img)

    shaft_w = max(3, int(s * 0.14))
    cx = s // 2
    shaft_top = int(s * 0.38)
    shaft_bot = int(s * 0.62)
    d.rounded_rectangle(
        [cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bot],
        radius=max(1, shaft_w // 3),
        fill=GREEN,
    )
    head_w = int(s * 0.42)
    head_top = int(s * 0.54)
    tip = int(s * 0.90)
    d.polygon(
        [
            (cx - head_w // 2, head_top),
            (cx + head_w // 2, head_top),
            (cx, tip),
        ],
        fill=GREEN,
    )
    if s >= 48:
        hw = max(1, shaft_w // 4)
        d.rounded_rectangle(
            [
                cx - shaft_w // 2 + 1,
                shaft_top + 2,
                cx - shaft_w // 2 + 1 + hw,
                shaft_bot - 2,
            ],
            radius=1,
            fill=GREEN_HI,
        )
    return img


def _rgba_to_bgra_dib(im: Image.Image) -> bytes:
    """BITMAPINFOHEADER + XOR mask (BGRA) + AND mask — классика ICO."""
    im = im.convert("RGBA")
    w, h = im.size
    # ICO stores bitmap bottom-up
    pixels = im.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        # rows already 4*w aligned
        xor += row

    # AND mask: 1 bit per pixel, padded to 32-bit rows
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = []
        for x in range(w):
            a = pixels[x, y][3]
            bits.append(1 if a < 128 else 0)
        # pack bits MSB first
        packed = bytearray(row_bytes)
        for i, bit in enumerate(bits):
            if bit:
                packed[i // 8] |= 0x80 >> (i % 8)
        and_mask += packed

    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        w,
        h * 2,  # height includes AND mask
        1,  # planes
        32,  # bit count
        0,  # BI_RGB
        len(xor),
        0,
        0,
        0,
        0,
    )
    return header + bytes(xor) + bytes(and_mask)


def write_bmp_ico(path: Path, images: list[Image.Image]) -> None:
    """Классический ICO только с BMP (без PNG-кадров)."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries: list[bytes] = []
    blobs: list[bytes] = []
    offset = 6 + 16 * count
    for im in images:
        blob = _rgba_to_bgra_dib(im)
        w, h = im.size
        wb = 0 if w >= 256 else w
        hb = 0 if h >= 256 else h
        entries.append(
            struct.pack(
                "<BBBBHHII",
                wb,
                hb,
                0,
                0,
                1,  # planes
                32,  # bitcount
                len(blob),
                offset,
            )
        )
        blobs.append(blob)
        offset += len(blob)
    path.write_bytes(header + b"".join(entries) + b"".join(blobs))


def main() -> None:
    (ROOT / "assets").mkdir(exist_ok=True)
    # Для exe/ярлыков достаточно этих размеров; 256 BMP большой, но ок
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw_icon(sz) for sz in sizes]
    images[-1].save(OUT_PREVIEW)
    write_bmp_ico(OUT_ICO, images)
    print(f"OK: {OUT_ICO} ({OUT_ICO.stat().st_size} bytes, BMP ICO)")
    print(f"OK: {OUT_PREVIEW}")


if __name__ == "__main__":
    main()
