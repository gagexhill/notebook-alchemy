"""The .ico packing in scripts/brand_icons.py. Rendering needs a browser and is not tested here."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brand_icons.py"
_spec = importlib.util.spec_from_file_location("brand_icons", _SCRIPT)
assert _spec and _spec.loader
brand_icons = importlib.util.module_from_spec(_spec)
sys.modules["brand_icons"] = brand_icons  # dataclasses look their module up while the class is built
_spec.loader.exec_module(brand_icons)


def fake_png(width: int, height: int | None = None) -> bytes:
    """Just enough of a PNG for its size to be read: the signature and the IHDR chunk."""
    ihdr = struct.pack(">II", width, height or width) + bytes(5)
    return brand_icons.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + ihdr + bytes(4)


def test_an_icon_holds_each_png_at_its_own_size() -> None:
    sizes = [16, 32, 48, 256]
    blob = brand_icons.pack_ico([fake_png(s) for s in sizes])
    assert brand_icons.read_ico(blob) == sizes


def test_256_px_is_written_as_zero_in_the_directory() -> None:
    blob = brand_icons.pack_ico([fake_png(256)])
    assert blob[6] == 0 and blob[7] == 0


def test_an_entry_that_holds_a_different_size_than_it_claims_is_rejected() -> None:
    blob = bytearray(brand_icons.pack_ico([fake_png(16), fake_png(32)]))
    blob[6 + 16] = 64  # the second entry now claims 64 px but holds a 32 px image
    with pytest.raises(ValueError, match="claims 64 px"):
        brand_icons.read_ico(bytes(blob))


def test_a_non_square_or_oversized_image_is_refused() -> None:
    with pytest.raises(ValueError, match="square"):
        brand_icons.pack_ico([fake_png(32, 16)])
    with pytest.raises(ValueError, match="at most 256"):
        brand_icons.pack_ico([fake_png(512)])
