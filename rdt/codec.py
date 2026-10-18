"""Bytes to typed values and back, for the scalar types a raw address can be read as.

The full codec (structs, enums, bit fields, Q formats, scaling) comes with the symbols in M3.
"""

from __future__ import annotations

import math
import struct

from .profile import parse_int

TYPES = {
    "u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i",
    "u64": "Q", "i64": "q", "f32": "f", "f64": "d",
}


def size_of(type_name: str) -> int:
    return struct.calcsize(_code(type_name))


def _code(type_name: str) -> str:
    try:
        return TYPES[type_name]
    except KeyError:
        raise ValueError(f"unknown type {type_name!r}; use one of {', '.join(TYPES)}") from None


def decode(data: bytes, type_name: str, byte_order: str) -> int | float:
    prefix = "<" if byte_order == "little" else ">"
    return struct.unpack(prefix + _code(type_name), data)[0]


def encode(value: int | float, type_name: str, byte_order: str) -> bytes:
    code = _code(type_name)
    prefix = "<" if byte_order == "little" else ">"
    if code in "fd":
        return struct.pack(prefix + code, float(value))
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{type_name} needs an integer, not {value}")
        value = int(value)
    bits = 8 * struct.calcsize(code)
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if code.islower() else (0, (1 << bits) - 1)
    if not lo <= value <= hi:
        raise ValueError(f"{value} does not fit in {type_name} ({lo}..{hi})")
    return struct.pack(prefix + code, value)


def parse_value(text: str, type_name: str) -> int | float:
    """Parse a value typed by the user for ``type_name``: 42, -7, 0x1F, 0b101, 1.5, 1e-3, nan, inf."""
    t = text.strip()
    if _code(type_name) in "fd":
        if t.lower().startswith(("0x", "-0x", "+0x")) and "." not in t and "p" not in t.lower():
            # A hex integer for a float is taken as its bit pattern, as the firmware sees it.
            raw = parse_int(t)
            bits = 32 if type_name == "f32" else 64
            if not 0 <= raw < 1 << bits:
                raise ValueError(f"{text!r} is not a {bits}-bit pattern for {type_name} "
                                 f"(0x0..0x{(1 << bits) - 1:X})")
            fmt = "<I" if type_name == "f32" else "<Q"
            return struct.unpack("<f" if type_name == "f32" else "<d", struct.pack(fmt, raw))[0]
        try:
            return float(t)
        except ValueError:
            raise ValueError(f"{text!r} is not a number") from None
    try:
        return parse_int(t)
    except ValueError:
        raise ValueError(f"{text!r} is not an integer") from None


def format_value(value: int | float, type_name: str, hex_too: bool = True) -> str:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return repr(value) if type_name == "f64" else f"{value:.9g}"
    if not hex_too:
        return str(value)
    width = 2 * size_of(type_name)
    raw = value & ((1 << (4 * width)) - 1)
    return f"{value} (0x{raw:0{width}X})"
