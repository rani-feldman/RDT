"""Display formats and scaling for one watched item.

The firmware holds a raw value; the user sees a scaled one:

    shown = (raw / 2**n) * k + offset      with n from the Q format (0 without one)

``Display`` turns a raw value read from the device into the value shown, and a value the user
types back into the raw value to write, refusing one that doesn't fit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .errors import ConfigError

FORMATS = ("dec", "hex", "bin", "char")
_Q = re.compile(r"^[Qq](?:(\d+)\.)?(\d+)$")


@dataclass
class Display:
    format: str = "dec"     # dec | hex | bin | char, for integers without scaling
    q: str | None = None    # Q15, Q31, Q3.12 ... : the raw value has this many fraction bits
    k: float = 1.0
    offset: float = 0.0
    unit: str = ""

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ConfigError(f"format must be one of {', '.join(FORMATS)}, not {self.format!r}")
        if self.q is not None and not _Q.match(self.q):
            raise ConfigError(f"Q format {self.q!r} must look like Q15, Q31 or Q3.12")
        if self.k == 0:
            raise ConfigError("scale k must not be 0")

    @property
    def fraction_bits(self) -> int:
        if not self.q:
            return 0
        return int(_Q.match(self.q).group(2))

    @property
    def scaled(self) -> bool:
        return bool(self.q) or self.k != 1.0 or self.offset != 0.0

    def to_shown(self, raw):
        """The value shown for a raw value. Unscaled integers stay integers."""
        if isinstance(raw, bool) or not self.scaled:
            return raw
        return (raw / (1 << self.fraction_bits)) * self.k + self.offset

    def to_raw(self, shown: float, is_float: bool):
        """The raw value to write for a value typed in shown units: a float for a float
        variable, else the nearest integer."""
        if not self.scaled:
            return shown
        raw = (float(shown) - self.offset) / self.k * (1 << self.fraction_bits)
        if is_float:
            return raw
        if math.isnan(raw) or math.isinf(raw):
            raise ValueError(f"{shown} has no raw value")
        return round(raw)

    def text(self, shown, bits: int | None = None) -> str:
        """``shown`` as text, in the item's format and with its unit."""
        if isinstance(shown, bool):
            out = "true" if shown else "false"
        elif isinstance(shown, float):
            out = str(shown) if math.isnan(shown) or math.isinf(shown) else f"{shown:.9g}"
        elif isinstance(shown, int) and self.format != "dec" and bits:
            raw = shown & ((1 << bits) - 1)
            if self.format == "hex":
                out = f"0x{raw:0{(bits + 3) // 4}X}"
            elif self.format == "bin":
                out = f"0b{raw:0{bits}b}"
            else:
                out = repr(chr(raw)) if 32 <= raw < 127 else f"0x{raw:02X}"
        else:
            out = str(shown)
        return f"{out} {self.unit}" if self.unit else out

    def to_dict(self) -> dict:
        d: dict = {}
        if self.format != "dec":
            d["format"] = self.format
        if self.q:
            d["q"] = self.q
        if self.k != 1.0:
            d["scale"] = _num(self.k)
        if self.offset != 0.0:
            d["offset"] = _num(self.offset)
        if self.unit:
            d["unit"] = self.unit
        return d

    @classmethod
    def from_dict(cls, t: dict) -> Display:
        try:
            return cls(str(t.get("format", "dec")), t.get("q"), float(t.get("scale", 1.0)),
                       float(t.get("offset", 0.0)), str(t.get("unit", "")))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"bad display settings {t!r}: {e}") from None


def _num(v: float) -> float | int:
    """A whole number written as an int in the workspace file: 2, not 2.0."""
    return int(v) if float(v).is_integer() else float(v)
