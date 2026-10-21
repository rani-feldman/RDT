"""SCPI profile: the user-defined command templates and reply format.

Nothing about the firmware's commands is hard-coded. The profile says how to spell a read, a
and a write. Addresses and values in commands are always bare hex with no
prefix: address 0x11AABBCCDD is sent as ``11AABBCCDD`` (rani, 2026-10-12). A read reply is one
bare hex value per 4-byte word, comma-separated, such as ``3FC00000,00000101`` (rani, 2026-10-13). Every read and write covers whole 4-byte aligned words (``align``, rani, 2026-10-12):
a 2-byte variable is read as its 4-byte word and trimmed. The simulator reads the same ``[scpi]`` table, so one file describes both sides.
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import BadReply, ConfigError, DeviceError

# Keys older profiles may still have, ignored on load (rani, 2026-10-12): there is no size query,
# since the largest read reply is the profile's max_response; and addresses, values and reply data
# are always bare hex, so their format keys are gone.
RETIRED_KEYS = {"size_query", "addr_format", "value_format", "data_format", "error_query", "probe", "reply_regex"}
# A width must have its leading 0: {addr:8X} would pad with spaces, which the firmware rejects (L5).
_HEX_SPEC = re.compile(r"^(0\d+)?X$")
WORD_SIZES = (1, 2, 4)   # allowed max_write and align, in bytes

TEMPLATE_FIELDS = {"read": {"mcu", "addr", "len"}, "write": {"mcu", "addr", "value"}}


RANGE_KINDS = ("peripheral", "memory")


@dataclass
class Range:
    """Memory RDT may access beyond the ELF's sections.

    ``kind`` says what it is:

    - ``peripheral`` (the default): registers, such as a GPIO block. Never merged with neighbouring
      reads, because reading a register can clear a flag or pop a FIFO; an access must stay inside
      it, and it may not overlap anything else.
    - ``memory``: plain RAM or flash, such as the whole of a RAM bank. It may overlap the ELF's
      sections, and reads in it are merged like reads in the ELF's sections.

    ``mcu`` limits the range to one MCU (by name or id); empty means every MCU.
    """

    name: str
    start: int
    size: int
    writable: bool = True
    mcu: str | int | None = None
    kind: str = "peripheral"

    @property
    def end(self) -> int:
        return self.start + self.size

    @classmethod
    def from_dict(cls, table: dict) -> Range:
        name = str(table.get("name", "range"))
        if ("size" in table) == ("end" in table):
            raise ConfigError(f"range {name}: give either size or end (the last address in it)")
        try:
            start = int(table["start"])
            size = int(table["size"]) if "size" in table else int(table["end"]) - start + 1
            r = cls(name=name, start=start, size=size, writable=bool(table.get("writable", True)),
                    mcu=table.get("mcu"), kind=str(table.get("kind", "peripheral")))
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"range {table!r}: needs integer start and size ({e})") from None
        if r.size < 1 or r.start < 0:
            raise ConfigError(f"range {r.name}: start must be >= 0 and size (or end) must leave at least 1 byte")
        if r.kind not in RANGE_KINDS:
            raise ConfigError(f"range {r.name}: kind must be {' or '.join(RANGE_KINDS)}, not {r.kind!r}")
        return r


def align_span(addr: int, size: int, align: int) -> tuple[int, int]:
    """The aligned span that holds ``size`` bytes at ``addr``: start rounded down and end rounded
    up to a multiple of ``align``. Returned as ``(start, end)``."""
    start = addr - addr % align
    end = -(-(addr + size) // align) * align
    return start, end


def fill_template(template: str, **values) -> str:
    """Fill a command template. ``{addr}`` and ``{value}`` are written as bare upper-case hex with
    no prefix. ``{addr}`` is exactly 8 digits, with leading zeros (08000000), since the firmware
    checks that every address has 8 characters, and ``{value}`` is unpadded. The only specs
    allowed are ``{addr:08X}`` and ``{value:X}``, which say the same thing."""
    out = []
    for literal, name, spec, _ in string.Formatter().parse(template):
        out.append(literal)
        if name is None:
            continue
        if name not in values:
            raise ConfigError(f"unknown placeholder {{{name}}} in {template!r}")
        value = values[name]
        if name in ("addr", "value"):
            if spec and not _HEX_SPEC.match(spec):
                raise ConfigError(f"{{{name}:{spec}}} in {template!r}: addresses and values are always "
                                  f"bare hex; only a zero-padded width such as {{{name}:08X}} is allowed")
            # The firmware takes an address as exactly 8 zero-padded digits and a value unpadded
            # (rani, 2026-10-14, SCPI-4), so no other width is allowed for either.
            if name == "addr" and spec not in ("", "08X"):
                raise ConfigError(f"{{addr:{spec}}} in {template!r}: the firmware needs every address "
                                  f"as exactly 8 zero-padded digits; use plain {{addr}} or {{addr:08X}}")
            if name == "value" and spec not in ("", "X"):
                raise ConfigError(f"{{value:{spec}}} in {template!r}: the firmware takes values unpadded; "
                                  f"use plain {{value}}")
            out.append(format(value, spec or ("08X" if name == "addr" else "X")))
        elif spec:
            out.append(format(value, spec))
        else:
            out.append(str(value))
    return "".join(out)


@dataclass
class Profile:
    name: str = "Default"
    read: str = "MEMory:READ? {mcu},{addr},{len}"
    write: str = "MEMory:WRITE {mcu},{addr},{value}"
    max_write: int = 4                     # largest value one write accepts, in bytes
    align: int = 4                         # every read and write covers whole aligned words of this size
    byte_order: str = "little"             # used for an MCU without an ELF
    max_response: int = 1024               # bytes of data one read reply can carry; set here only
    terminator: str = "\n"
    timeout_ms: float = 500
    retries: int = 2                       # extra attempts after a read times out
    ranges: list[Range] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.byte_order not in ("little", "big"):
            raise ConfigError(f"byte_order must be little or big, not {self.byte_order!r}")
        if self.max_response < 1:
            raise ConfigError("max_response must be at least 1")
        # 8 was dropped for both: the firmware is 32-bit and stores at most 4 bytes, so an 8-byte
        # value is always two word writes (rani, 2026-10-21, SCPI-13).
        if self.max_write not in WORD_SIZES:
            raise ConfigError("max_write must be 1, 2 or 4")
        if self.align not in WORD_SIZES:
            raise ConfigError("align must be 1, 2 or 4")
        if self.max_write < self.align:
            raise ConfigError(f"max_write ({self.max_write}) must be at least align ({self.align})")
        if self.max_response < self.align:
            raise ConfigError(f"max_response ({self.max_response}) must be at least align ({self.align})")
        if not self.terminator:
            raise ConfigError("terminator must not be empty")
        if self.timeout_ms <= 0:
            raise ConfigError("timeout_ms must be positive")
        if self.retries < 0:
            raise ConfigError("retries must be 0 or more")
        for key, allowed in TEMPLATE_FIELDS.items():
            names = {n for _, n, _, _ in string.Formatter().parse(getattr(self, key)) if n is not None}
            if key == "write" and "size" in names:
                # Every write is one max_write-byte word, so the firmware needs no size (rani,
                # 2026-10-21, SCPI-13).
                raise ConfigError("write template: {size} was removed; every write is max_write bytes, "
                                  "so take ,{size} out of the template")
            if names - allowed:
                raise ConfigError(f"{key} template uses unknown placeholders: {', '.join(sorted(names - allowed))}")
            if key == "read" and not {"addr", "len"} <= names:
                raise ConfigError("read template needs {addr} and {len}")
            if key == "write" and not {"addr", "value"} <= names:
                raise ConfigError("write template needs {addr} and {value}")
        # Catch format typos now rather than on the first command.
        self.format_read(0, 0, 1)
        self.format_write(0, 0, 0)

    # --- loading -----------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except OSError as e:
            raise ConfigError(f"cannot read profile {path}: {e.strerror}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"profile {path}: {e}") from None
        return cls.from_dict(raw.get("scpi", {}), raw.get("ranges", []))

    @classmethod
    def from_dict(cls, table: dict, ranges: list[dict] | None = None) -> Profile:
        known = {f.name for f in fields(cls)} - {"ranges"}
        table = {k: v for k, v in table.items() if k not in RETIRED_KEYS}
        unknown = set(table) - known
        if unknown:
            raise ConfigError(f"unknown [scpi] keys: {', '.join(sorted(unknown))}")
        for f in fields(cls):   # a config error rather than a TypeError later (L6)
            if f.name in table and f.name != "ranges":
                ok = _TYPES[f.type]
                v = table[f.name]
                if isinstance(v, bool) or not isinstance(v, ok):
                    want = "a number" if f.type == "float" else "an integer" if f.type == "int" else "a string"
                    raise ConfigError(f"[scpi] {f.name} must be {want}, not {v!r}")
        return cls(**table, ranges=[Range.from_dict(r) for r in ranges or []])

    # --- commands ----------------------------------------------------------------------------

    def format_read(self, mcu: int, addr: int, length: int) -> str:
        return self._fill(self.read, mcu=mcu, addr=addr, len=length)

    def format_write(self, mcu: int, addr: int, value: int) -> str:
        return self._fill(self.write, mcu=mcu, addr=addr, value=value)

    def _fill(self, template: str, **kw) -> str:
        try:
            return fill_template(template, **kw)
        except (ValueError, KeyError, IndexError) as e:
            raise ConfigError(f"cannot fill {template!r}: {e}") from None

    # --- replies -----------------------------------------------------------------------------

    def decode_data(self, reply: bytes, length: int, byte_order: str | None = None) -> bytes:
        """Decode a read reply (without its terminator) into exactly ``length`` bytes.

        The reply is one bare hex value per aligned word (``align`` bytes), lowest address first,
        separated by commas, such as ``3FC00000,00000101`` (rani, 2026-10-13: the firmware prints
        each word as a ``uint32_t``). Each value is turned into bytes in the MCU's ``byte_order``.
        """
        text = reply.decode("latin-1").strip()
        data = decode_words(text, self.align, byte_order or self.byte_order)
        if len(data) != length:
            raise BadReply(f"expected {length} bytes, got {len(data)}")
        return data


_TYPES = {"int": int, "float": (int, float), "str": str}

_WORD = re.compile(r"^[0-9A-Fa-f]+$")


def decode_words(text: str, size: int, byte_order: str) -> bytes:
    """Comma-separated bare hex values of ``size`` bytes each, such as ``3FC00000,00000101``."""
    out = bytearray()
    for token in text.split(","):
        t = token.strip()
        if not _WORD.match(t) or len(t) > 2 * size:
            raise BadReply(f"reply {text[:60]!r} is not comma-separated bare hex words of {size} bytes")
        out += int(t, 16).to_bytes(size, byte_order)
    return bytes(out)


_ERROR = re.compile(r'^\s*([+-]?\d+)\s*(?:,\s*"?(.*?)"?\s*)?$')


def parse_error(reply: str) -> DeviceError | None:
    """Parse an error query reply. Returns None for ``0,"No error"``."""
    m = _ERROR.match(reply)
    if not m:
        raise BadReply(f"error query reply {reply[:60]!r} is not 'code,\"text\"'")
    code = int(m.group(1))
    return None if code == 0 else DeviceError(code, m.group(2) or "")


def parse_int(text: str) -> int:
    """Parse a number as the user types it or SCPI sends it: decimal, ``0x..``, ``0b..``, ``#H..``."""
    t = text.strip().replace("_", "")
    u = t.upper()
    for prefix, base in (("#H", 16), ("#B", 2), ("#Q", 8)):
        if u.startswith(prefix):
            return int(t[2:], base)
    return int(t, 0) if u.lstrip("+-")[:2] in ("0X", "0B", "0O") else int(t, 10)


def load_memory_file(path: str | Path) -> list[Range]:
    """The memory map in a memory file: its ``[[ranges]]`` (each may name an ``mcu``). A profile,
    target file or workspace works too, so a map can be taken from any of them: its ``[[ranges]]``
    and each ``[[mcu]]``'s ``[[mcu.ranges]]`` (the latter tied to that MCU)."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read memory file {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"memory file {path}: {e}") from None
    out = [Range.from_dict(r) for r in raw.get("ranges", [])]
    for m in raw.get("mcu", []):
        for r in m.get("ranges", []):
            out.append(Range.from_dict({"mcu": m.get("name", m.get("id")), **r}))
    for key, loader in (("profile", Profile.load), ("memory_file", load_memory_file)):   # a workspace points to files
        if isinstance(raw.get(key), str):
            out += [r for r in (loader(Path(path).parent / raw[key]) if key == "memory_file"
                                else loader(Path(path).parent / raw[key]).ranges) if r not in out]
    return out
