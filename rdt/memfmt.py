"""Display formats of the memory view: how a run of raw bytes is cut into cells and shown as text,
and how a typed cell is turned back into bytes. Qt-free, so the GUI and the tests share it.

A format is an element kind (unsigned, signed, ascii, float, double), an element size in bytes, and
for the integer kinds a radix (hex, or decimal when the Decimal option is on). C type sizes are the
ARM ones: Char 8 bits, Short 16, Int 32, Long 32.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# menu name -> size in bytes, in the order of the menu
INT_TYPES = {"Char": 1, "Int": 4, "Short": 2, "Long": 4}
KINDS = ("unsigned", "signed", "ascii", "float", "double")


@dataclass(frozen=True)
class MemFormat:
    kind: str = "unsigned"
    type: str = "Char"        # integer kinds only: a key of INT_TYPES
    decimal: bool = False     # integer kinds only

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown memory format {self.kind!r}")
        if self.type not in INT_TYPES:
            raise ValueError(f"unknown C type {self.type!r}")

    @property
    def size(self) -> int:
        if self.kind in ("unsigned", "signed"):
            return INT_TYPES[self.type]
        return {"ascii": 1, "float": 4, "double": 8}[self.kind]

    @property
    def label(self) -> str:
        if self.kind in ("unsigned", "signed"):
            return f"{self.kind.capitalize()} {self.type}" + (", decimal" if self.decimal else "")
        return self.kind.capitalize()

    def to_text(self, raw: bytes, order: str) -> str:
        """One cell: `raw` is `size` bytes in memory order."""
        if self.kind == "ascii":
            return chr(raw[0]) if 32 <= raw[0] < 127 else "."
        if self.kind in ("float", "double"):
            (v,) = struct.unpack(("<" if order == "little" else ">") + ("f" if self.kind == "float" else "d"), raw)
            return f"{v:.{7 if self.kind == 'float' else 15}g}"
        v = int.from_bytes(raw, order, signed=self.kind == "signed")
        if self.decimal:
            return str(v)
        return f"{'-' if v < 0 else ''}{abs(v):0{2 * self.size}X}"

    def to_ascii(self, raw: bytes, order: str) -> str:
        """The ASCII column for one cell: its bytes as characters, in the order the value is read
        (most significant byte first), so they line up with the hex digits of the cell."""
        if self.size > 1 and order == "little":
            raw = raw[::-1]
        return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)

    def from_text(self, text: str, order: str) -> bytes:
        """The bytes (memory order) for a typed cell; raises ValueError with a message for the user."""
        text = text.strip()
        if self.kind == "ascii":
            if len(text) != 1 or ord(text) > 0xFF:
                raise ValueError(f"{text!r} is not one character")
            return text.encode("latin-1")
        if self.kind in ("float", "double"):
            try:
                v = float(text)
            except ValueError:
                raise ValueError(f"{text!r} is not a number") from None
            fmt = ("<" if order == "little" else ">") + ("f" if self.kind == "float" else "d")
            try:
                return struct.pack(fmt, v)
            except OverflowError:
                raise ValueError(f"{text} does not fit in a {self.kind}") from None
        try:
            v = int(text, 10 if self.decimal else 16) if not text.lower().lstrip("-").startswith("0x") \
                else int(text, 16)
        except ValueError:
            raise ValueError(f"{text!r} is not a {'decimal' if self.decimal else 'hex'} number") from None
        signed = self.kind == "signed"
        try:
            return v.to_bytes(self.size, order, signed=signed)
        except OverflowError:
            bits = 8 * self.size
            lo, hi = (-(1 << bits - 1), (1 << bits - 1) - 1) if signed else (0, (1 << bits) - 1)
            raise ValueError(f"{text} is out of range for {self.label} ({lo}..{hi})") from None
