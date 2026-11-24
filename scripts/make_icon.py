"""Generate the app icons (PNG + ICO) used for EXE packaging and the tray.

Usage::

    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def draw(size: int):
    from PIL import Image, ImageDraw

    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def s(value: float) -> int:
        return int(round(value * scale))

    # body: rounded-square blue base
    draw.rounded_rectangle((s(4), s(10), s(60), s(56)), radius=s(12), fill=(47, 107, 255, 255))
    # highlight
    draw.rounded_rectangle((s(4), s(10), s(60), s(34)), radius=s(12), fill=(78, 133, 255, 255))
    draw.rectangle((s(4), s(24), s(60), s(34)), fill=(78, 133, 255, 255))

    # SD card slot
    draw.rounded_rectangle((s(16), s(20), s(48), s(30)), radius=s(3), fill=(255, 255, 255, 255))
    for index in range(4):
        x = s(19 + index * 6)
        draw.rectangle((x, s(22), x + s(4), s(28)), fill=(47, 107, 255, 255))

    # downward arrow
    draw.line((s(32), s(34), s(32), s(46)), fill=(255, 255, 255, 255), width=s(4))
    draw.polygon([(s(24), s(41)), (s(40), s(41)), (s(32), s(50))], fill=(255, 255, 255, 255))

    return image


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw(size) for size in sizes]

    images[0].save(ASSETS / "dsync.png")
    images[-1].save(ASSETS / "dsync_256.png")

    # ICO: combine several sizes
    base = draw(256)
    base.save(ASSETS / "dsync.ico", format="ICO",
               sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    print(f"Icons generated: {ASSETS}")
    for path in sorted(ASSETS.iterdir()):
        print(f"  {path.name}  ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
