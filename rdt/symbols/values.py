"""Typed values: bytes read from the device to Python values and display text, and back.

``rows()`` flattens a value into one row per field and cell, the way the variable view and
``rdt read`` show it: structs open to every field and arrays to every cell, nested to any depth.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Callable

from ..profile import parse_int
from .types import ArrayType, BaseType, EnumType, PointerType, StructType, Type, is_char, is_char_array, is_scalar

Symbolizer = Callable[[int, "Type | None"], "str | None"]   # (address, pointed-to type)


class EnumValue(int):
    """An enum's value: an int that also knows its enumerator's name (None if it has none)."""

    name: str | None

    def __new__(cls, value: int, name: str | None):
        obj = super().__new__(cls, value)
        obj.name = name
        return obj

    def __repr__(self) -> str:
        return f"{self.name} ({int(self)})" if self.name else str(int(self))


# --- scalars ---------------------------------------------------------------------------------

def _sign(raw: int, bits: int) -> int:
    return raw - (1 << bits) if raw >> (bits - 1) & 1 else raw


def _is_signed(t: Type) -> bool:
    return (isinstance(t, BaseType) and t.signed) or (isinstance(t, EnumType) and t.signed)


def decode_scalar(t: Type, data: bytes, order: str, bit_size: int | None = None, bit_shift: int = 0):
    t = t.strip()
    raw = int.from_bytes(data, order)
    bits = 8 * len(data)
    if bit_size is not None:
        raw = (raw >> bit_shift) & ((1 << bit_size) - 1)
        bits = bit_size
    if isinstance(t, BaseType) and t.kind == "float" and bit_size is None:
        if len(data) == 4:
            return struct.unpack(("<" if order == "little" else ">") + "f", data)[0]
        if len(data) == 8:
            return struct.unpack(("<" if order == "little" else ">") + "d", data)[0]
        return raw   # a long double the host can't represent: shown as its bits
    if isinstance(t, BaseType) and t.kind == "bool":
        return bool(raw)
    value = _sign(raw, bits) if _is_signed(t) else raw
    if isinstance(t, EnumType):
        return EnumValue(value, t.values.get(value))
    return value


def decode(t: Type, data: bytes, order: str) -> Any:
    """Decode a whole value: a dict for a struct, a list for an array, else a scalar."""
    s = t.strip()
    if isinstance(s, StructType):
        out = {}
        for off, m in s.named_members():
            chunk = data[off:off + (m.storage if m.is_bitfield else m.type.size)]
            if m.is_bitfield:
                out[m.name] = decode_scalar(m.type, chunk, order, m.bit_size, m.bit_shift)
            else:
                out[m.name] = decode(m.type, chunk, order)
        return out
    if is_char_array(s):
        return bytes(data)
    if isinstance(s, ArrayType):
        n, size = s.count or 0, s.element.size
        return [decode(s.element, data[i * size:(i + 1) * size], order) for i in range(n)]
    if is_scalar(s) and len(data) == s.size:
        return decode_scalar(s, data, order)
    return bytes(data)


def format_scalar(t: Type, value, bit_size: int | None = None, symbolize: Symbolizer | None = None,
                  hex_too: bool = True) -> str:
    s = t.strip()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return repr(value) if s.size == 8 else f"{value:.9g}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, EnumValue):
        if not value.name:
            return str(int(value))
        return f"{value.name} ({int(value)})" if hex_too else value.name
    if isinstance(s, PointerType):
        width = 2 * s.size
        text = f"0x{value:0{width}X}"
        if value == 0:
            return text + " (NULL)"
        where = symbolize(value, s.target) if symbolize else None
        return f"{text} ({where})" if where else text
    if is_char(t) and bit_size is None:
        ch = value & 0xFF
        return f"{value} {_char_literal(ch)}" if 32 <= ch < 127 else (f"{value} (0x{ch:02X})" if hex_too else str(value))
    if not hex_too or not isinstance(value, int):
        return str(value)
    bits = bit_size if bit_size is not None else 8 * s.size
    width = max(1, (bits + 3) // 4)
    return f"{value} (0x{value & ((1 << bits) - 1):0{width}X})"


def _char_literal(ch: int) -> str:
    c = chr(ch)
    return "'\\''" if c == "'" else "'\\\\'" if c == "\\" else f"'{c}'"


def c_string(data: bytes) -> str:
    """A char array as a C string literal, up to its first NUL."""
    end = data.find(0)
    body = data if end < 0 else data[:end]
    out = []
    for b in body:
        c = chr(b)
        if c in '"\\':
            out.append("\\" + c)
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append({10: "\\n", 13: "\\r", 9: "\\t"}.get(b, f"\\x{b:02X}"))
    return '"' + "".join(out) + '"' + ("" if end >= 0 else " (no NUL)")


def parse_scalar(t: Type, text: str, bit_size: int | None = None):
    """Parse a value typed by the user for a scalar type, and check that it fits."""
    s = t.strip()
    raw = text.strip()
    if isinstance(s, BaseType) and s.kind == "float":
        from .. import codec
        if s.size not in (4, 8):
            raise ValueError(f"cannot write a {s.size}-byte float")
        return codec.parse_value(raw, "f32" if s.size == 4 else "f64")
    if isinstance(s, BaseType) and s.kind == "bool":
        low = raw.lower()
        if low in ("true", "on", "1"):
            return True
        if low in ("false", "off", "0"):
            return False
        raise ValueError(f"{text!r} is not a bool; use true or false")
    value = None
    if isinstance(s, EnumType):
        value = s.value_of(raw)
        if value is None and not _looks_numeric(raw):
            names = ", ".join(s.values.values())
            raise ValueError(f"{raw!r} is not a name of {t.name}" + (f"; use one of {names}" if names else ""))
    elif is_char(t) and len(raw) == 3 and raw[0] == raw[2] == "'":
        value = ord(raw[1])
    if value is None:
        try:
            value = parse_int(raw)
        except ValueError:
            raise ValueError(f"{text!r} is not an integer") from None
    if not isinstance(s, (BaseType, EnumType, PointerType)):
        raise ValueError(f"cannot write a {t.name}")
    bits = bit_size if bit_size is not None else 8 * s.size
    signed = _is_signed(s)
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= value <= hi:
        what = f"a {bits}-bit field" if bit_size is not None else t.name
        raise ValueError(f"{value} does not fit in {what} ({lo}..{hi})")
    return value


def _looks_numeric(text: str) -> bool:
    try:
        parse_int(text)
        return True
    except ValueError:
        return False


def encode_scalar(t: Type, value, order: str, size: int, bit_size: int | None = None, bit_shift: int = 0,
                  old: bytes | None = None) -> bytes:
    """Bytes for a scalar; for a bit field, ``old`` (the bytes holding it) with the field replaced."""
    s = t.strip()
    if isinstance(s, BaseType) and s.kind == "float" and bit_size is None:
        return struct.pack(("<" if order == "little" else ">") + ("f" if size == 4 else "d"), float(value))
    value = int(value)
    if bit_size is None:
        return (value & ((1 << 8 * size) - 1)).to_bytes(size, order)
    mask = ((1 << bit_size) - 1) << bit_shift
    word = int.from_bytes(old, order) if old is not None else 0
    word = (word & ~mask) | ((value << bit_shift) & mask)
    return word.to_bytes(size, order)


# --- rows ------------------------------------------------------------------------------------

@dataclass
class Row:
    path: str           # the full expression, such as main:dev.corners[1].x
    depth: int
    type: Type
    addr: int
    text: str | None    # None for a struct or array header; its fields follow
    value: Any = None
    bit_size: int | None = None

    @property
    def type_name(self) -> str:
        return f"{self.type.name} : {self.bit_size}" if self.bit_size is not None else self.type.name


def child_base(path: str) -> str:
    """The text a member or index is appended to: ``*p`` must become ``(*p)`` first."""
    mcu, sep, expr = path.rpartition(":")
    return f"{mcu}{sep}({expr})" if expr.startswith("*") else path


def rows(t: Type, data: bytes, order: str, path: str, addr: int, max_cells: int = 100,
         symbolize: Symbolizer | None = None, bit_size: int | None = None, bit_shift: int = 0) -> list[Row]:
    out: list[Row] = []
    _rows(out, t, data, order, path, addr, 0, max_cells, symbolize, bit_size, bit_shift)
    return out


def _rows(out, t, data, order, path, addr, depth, max_cells, symbolize, bit_size=None, bit_shift=0):
    s = t.strip()
    if bit_size is not None or is_scalar(s):
        v = decode_scalar(s, data, order, bit_size, bit_shift)
        out.append(Row(path, depth, t, addr, format_scalar(t, v, bit_size, symbolize), v, bit_size))
    elif is_char_array(s):
        out.append(Row(path, depth, t, addr, c_string(data), bytes(data)))
    elif isinstance(s, StructType):
        out.append(Row(path, depth, t, addr, None))
        base = child_base(path)
        for off, m in s.named_members():
            size = m.storage if m.is_bitfield else m.type.size
            _rows(out, m.type, data[off:off + size], order, f"{base}.{m.name}", addr + off, depth + 1,
                  max_cells, symbolize, m.bit_size, m.bit_shift)
    elif isinstance(s, ArrayType):
        out.append(Row(path, depth, t, addr, None))
        base = child_base(path)
        n, size = s.count or 0, s.element.size
        for i in range(min(n, max_cells)):
            _rows(out, s.element, data[i * size:(i + 1) * size], order, f"{base}[{i}]", addr + i * size,
                  depth + 1, max_cells, symbolize)
        if n > max_cells:
            out.append(Row(f"{base}[{max_cells}..{n - 1}]", depth + 1, s.element, addr + max_cells * size,
                           f"... {n - max_cells} more cells"))
    else:
        out.append(Row(path, depth, t, addr, data.hex(" ").upper() or "(no data)", bytes(data)))
