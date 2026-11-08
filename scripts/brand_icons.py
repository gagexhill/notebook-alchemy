"""Render the icon package in docs/brand/icons/ from docs/brand/icon.svg with Playwright.

Playwright comes with the `notebooklm-py[browser]` dependency; install its browser once with
`python -m playwright install chromium`. Chromium draws every PNG from the one SVG, and the .ico files
are packed here from those PNGs (PNG-compressed entries, which Windows Vista and later and every current
browser read), so no image library is needed.

Usage: python scripts/brand_icons.py [--source docs/brand/icon.svg] [--out docs/brand/icons]
"""

from __future__ import annotations

import argparse
import base64
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "docs" / "brand" / "icon.svg"
DEFAULT_OUT = ROOT / "docs" / "brand" / "icons"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Opaque icons (iOS home screen, Android adaptive) need a filled square; the mark sits on white.
BACKGROUND = "#FFFFFF"
# Android masks an adaptive icon to any shape inside a circle of 80% of its width, so the mark is
# scaled to 68%: its bounding box (180 x 131 of 192 units) then fits that circle with room to spare.
MASKABLE_SCALE = 0.68
# iOS rounds the corners of the apple-touch icon itself; 76% keeps the arches clear of them.
APPLE_SCALE = 0.76
FAVICON_SIZES = (16, 32, 48)
# Windows picks the nearest size for each view and display scale: Explorer lists use 16-32, the desktop
# 32-64 (more at high DPI), large and extra-large views 96-256.
WINDOWS_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


@dataclass(frozen=True)
class Raster:
    name: str
    size: int
    scale: float = 1.0
    background: str | None = None  # None leaves the background transparent


RASTERS = (
    *(Raster(f"favicon-{s}.png", s) for s in FAVICON_SIZES),
    Raster("apple-touch-icon.png", 180, APPLE_SCALE, BACKGROUND),
    Raster("icon-192.png", 192),
    Raster("icon-512.png", 512),
    Raster("icon-maskable-512.png", 512, MASKABLE_SCALE, BACKGROUND),
)


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk."""
    if png[:8] != PNG_SIGNATURE or png[12:16] != b"IHDR":
        raise ValueError("not a PNG image")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def pack_ico(images: list[bytes]) -> bytes:
    """An .ico file holding each PNG as one entry, in the order given."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for png in images:
        width, height = png_size(png)
        if width != height or not 1 <= width <= 256:
            raise ValueError(f"an icon entry must be square and at most 256 px, got {width}x{height}")
        side = 0 if width == 256 else width  # the directory stores 256 as 0
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def read_ico(blob: bytes) -> list[int]:
    """The pixel size of each entry, after checking that every entry is a PNG of the size it claims."""
    reserved, kind, count = struct.unpack("<HHH", blob[:6])
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError("not an .ico file")
    sizes = []
    for i in range(count):
        side, _, _, _, _, _, length, offset = struct.unpack("<BBBBHHII", blob[6 + 16 * i : 22 + 16 * i])
        width, height = png_size(blob[offset : offset + length])
        expected = side or 256
        if (width, height) != (expected, expected) or offset + length > len(blob):
            raise ValueError(f"entry {i} claims {expected} px but holds {width}x{height}")
        sizes.append(width)
    return sizes


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{size}px;height:{size}px;background:{background};overflow:hidden}}
img{{position:absolute;left:{offset}px;top:{offset}px;width:{inner}px;height:{inner}px}}
</style></head><body><img src="{src}" alt=""></body></html>"""


def render(svg: bytes, rasters: list[Raster]) -> dict[str, bytes]:
    from playwright.sync_api import sync_playwright  # imported here so the packing code needs no browser

    src = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=1)
            for r in rasters:
                inner = round(r.size * r.scale)
                html = PAGE.format(
                    size=r.size,
                    inner=inner,
                    offset=(r.size - inner) / 2,
                    background=r.background or "transparent",
                    src=src,
                )
                page.set_viewport_size({"width": r.size, "height": r.size})
                page.set_content(html)
                page.wait_for_function("document.images[0].complete && document.images[0].naturalWidth > 0")
                png = page.screenshot(type="png", omit_background=r.background is None)
                if png_size(png) != (r.size, r.size):
                    raise RuntimeError(f"{r.name}: rendered {png_size(png)}, expected {r.size} px")
                out[r.name] = png
        finally:
            browser.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    svg = args.source.read_bytes()
    windows = [Raster(f"windows-{s}.png", s) for s in WINDOWS_SIZES]
    pngs = render(svg, [*RASTERS, *windows])

    files = {r.name: pngs[r.name] for r in RASTERS}
    files["favicon.svg"] = svg
    files["favicon.ico"] = pack_ico([pngs[f"favicon-{s}.png"] for s in FAVICON_SIZES])
    files["notebook-alchemy.ico"] = pack_ico([pngs[r.name] for r in windows])
    for name in ("favicon.ico", "notebook-alchemy.ico"):
        read_ico(files[name])  # raises if an entry does not hold what its directory says

    args.out.mkdir(parents=True, exist_ok=True)
    for name, blob in sorted(files.items()):
        (args.out / name).write_bytes(blob)
        print(f"wrote {args.out / name} ({len(blob)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
