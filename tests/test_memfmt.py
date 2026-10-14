import struct

import pytest

from rdt.memfmt import MemFormat


def test_default_is_hex_bytes():
    f = MemFormat()
    assert f.size == 1 and f.to_text(b"\x7f", "little") == "7F"
    assert f.from_text("a5", "little") == b"\xa5"


@pytest.mark.parametrize("kind,type,decimal,raw,text", [
    ("unsigned", "Short", False, b"\x34\x12", "1234"),
    ("unsigned", "Long", False, b"\x78\x56\x34\x12", "12345678"),
    ("unsigned", "Int", True, b"\xff\xff\xff\xff", "4294967295"),
    ("signed", "Char", True, b"\xfe", "-2"),
    ("signed", "Short", False, b"\xfe\xff", "-0002"),
    ("signed", "Long", True, b"\x00\x00\x00\x80", "-2147483648"),
])
def test_integers_round_trip(kind, type, decimal, raw, text):
    f = MemFormat(kind, type, decimal)
    assert f.to_text(raw, "little") == text
    assert f.from_text(text, "little") == raw


def test_big_endian():
    assert MemFormat("unsigned", "Short").to_text(b"\x12\x34", "big") == "1234"


def test_float_and_double():
    f, d = MemFormat("float"), MemFormat("double")
    assert (f.size, d.size) == (4, 8)
    assert f.to_text(struct.pack("<f", 1.5), "little") == "1.5"
    assert f.from_text("-0.25", "little") == struct.pack("<f", -0.25)
    assert d.to_text(struct.pack("<d", 3.141592653589793), "little") == "3.14159265358979"
    with pytest.raises(ValueError):
        f.from_text("1e40", "little")


def test_ascii():
    a = MemFormat("ascii")
    assert a.to_text(b"A", "little") == "A" and a.to_text(b"\x01", "little") == "."
    assert a.from_text("z", "little") == b"z"
    with pytest.raises(ValueError):
        a.from_text("ab", "little")


def test_out_of_range_is_refused():
    with pytest.raises(ValueError, match="out of range"):
        MemFormat("unsigned", "Char", True).from_text("256", "little")
    with pytest.raises(ValueError, match="out of range"):
        MemFormat("signed", "Char", True).from_text("-129", "little")
    with pytest.raises(ValueError, match="not a hex"):
        MemFormat("unsigned", "Short").from_text("xyz", "little")


def test_ascii_column_follows_the_value_order():
    raw = b"\x03\x00\x00\x30"                  # 0x30000003 in little-endian memory
    long = MemFormat("unsigned", "Long")
    assert long.to_text(raw, "little") == "30000003"
    assert long.to_ascii(raw, "little") == "0..."
    assert long.to_ascii(b"\x30\x00\x00\x03", "big") == "0..."
    assert MemFormat().to_ascii(b"A", "little") == "A"
    assert MemFormat("unsigned", "Short").to_ascii(b"AB", "little") == "BA"
