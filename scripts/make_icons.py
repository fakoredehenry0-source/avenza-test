"""Generate the PWA icons.

Run from the project root:  python scripts/make_icons.py
Requires Pillow (development-only dependency; not needed at runtime).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icons"


def _font(size: int):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_icon(size: int, maskable: bool = False) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = int(size * (0.10 if maskable else 0.06))
    radius = int(size * (0.30 if maskable else 0.24))
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=(255, 211, 77, 255),
    )

    # Subtle inner ring for depth.
    ring = max(2, size // 64)
    draw.rounded_rectangle(
        [pad + ring * 3, pad + ring * 3, size - pad - ring * 3, size - pad - ring * 3],
        radius=max(4, radius - ring * 3),
        outline=(255, 159, 26, 180),
        width=ring,
    )

    letter = "A"
    font = _font(int(size * (0.56 if maskable else 0.62)))
    box = draw.textbbox((0, 0), letter, font=font)
    text_w = box[2] - box[0]
    text_h = box[3] - box[1]
    draw.text(
        ((size - text_w) / 2 - box[0], (size - text_h) / 2 - box[1]),
        letter,
        font=font,
        fill=(22, 16, 0, 255),
    )

    # A small waveform bar under the letter to signal voice.
    bar_w = max(3, size // 46)
    gap = max(4, size // 40)
    heights = [0.10, 0.16, 0.22, 0.16, 0.10]
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    start_x = (size - total_w) / 2
    base_y = int(size * (0.74 if maskable else 0.78))
    for index, factor in enumerate(heights):
        h = int(size * factor)
        x = start_x + index * (bar_w + gap)
        draw.rounded_rectangle(
            [x, base_y - h, x + bar_w, base_y],
            radius=bar_w // 2,
            fill=(22, 16, 0, 220),
        )
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_icon(192).save(OUT / "icon-192.png")
    make_icon(512).save(OUT / "icon-512.png")
    make_icon(512, maskable=True).save(OUT / "icon-maskable-512.png")
    # Apple touch icon uses the plain 180px asset.
    make_icon(180).save(OUT / "apple-touch-icon.png")
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()
