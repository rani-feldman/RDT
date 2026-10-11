"""SCPI profile: the user-defined command templates and reply format.

Nothing about the firmware's commands is hard-coded. The profile says how to spell a read, a
write, the error query and the size query, and how the read reply is encoded. The simulator
reads the same ``[scpi]`` table, so one file describes both sides.
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import BadReply, ConfigError, DeviceError

DATA_FORMATS = ("hex", "bytes_csv", "decimal", "ieee_block")
TEMPLATE_FIELDS = {"read": {"mcu", "addr", "len"}, "write": {"mcu", "addr", "value", "size"}}


@dataclass
class Range:
    """Memory with no ELF section behind it, such as a peripheral block.

    Ranges are never merged with neighbouring reads, because reading a register can clear a flag
    or pop a FIFO. ``mcu`` limits the range to one MCU (by name or id); empty means every MCU.
    """

    name: str
    start: int
    size: int
    writable: bool = True
    mcu: str | int | None = None

    @property
    def end(self) -> int:
        return self.start + self.size

    @classmethod
    def from_dict(cls, table: dict) -> Range:
        try:
            r = cls(name=str(table.get("name", "range")), start=int(table["start"]), size=int(table["size"]),
                    writable=bool(table.get("writable", True)), mcu=table.get("mcu"))
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"range {table!r}: needs integer start and size ({e})") from None
        if r.size < 1 or r.start < 0:
            raise ConfigError(f"range {r.name}: start must be >= 0 and size >= 1")
        return r


def fill_template(template: str, addr_format: str, value_format: str, **values) -> str:
    """Fill a command template. ``{addr}`` and ``{value}`` use the profile's formats unless the
    template gives its own spec, as in ``{addr:08X}``."""
    out = []
    for literal, name, spec, _ in string.Formatter().parse(template):
        out.append(literal)
        if name is None:
            continue
        if name not in values:
            raise ConfigError(f"unknown placeholder {{{name}}} in {template!r}")
        value = values[name]
        if spec:
            out.append(format(value, spec))
        elif name == "addr":
            out.append(addr_format.format(addr=value))
        elif name == "value":
            out.append(value_format.format(value=value))
        else:
            out.append(str(value))
    return "".join(out)


@dataclass
class Profile:
    name: str = "Default"
    read: str = "MEMory:READ? {mcu},{addr},{len}"
    write: str = "MEMory:WRITE {mcu},{addr},{value},{size}"
    value_format: str = "0x{value:X}"      # raw integer; floats are sent as their bit pattern
    addr_format: str = "0x{addr:08X}"
    max_write: int = 4                     # largest value one write accepts, in bytes
    size_query: str | None = "MEMory:MAX?"  # optional; its reply replaces max_response on connect
    data_format: str = "hex"               # read reply: hex | bytes_csv | decimal | ieee_block
    reply_regex: str | None = None         # optional; group "data" (or group 1) holds the data
    byte_order: str = "little"             # used for an MCU without an ELF
    max_response: int = 1024               # bytes of data one read reply can carry
    error_query: str | None = "SYSTem:ERRor?"
    probe: str | None = "*IDN?"            # sent on connect and reconnect to check the device answers
    terminator: str = "\n"
    timeout_ms: float = 500
    retries: int = 2                       # extra attempts after a read times out
    ranges: list[Range] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.data_format not in DATA_FORMATS:
            raise ConfigError(f"data_format must be one of {', '.join(DATA_FORMATS)}, not {self.data_format!r}")
        if self.byte_order not in ("little", "big"):
            raise ConfigError(f"byte_order must be little or big, not {self.byte_order!r}")
        if self.max_response < 1:
            raise ConfigError("max_response must be at least 1")
        if self.max_write not in (1, 2, 4, 8):
            raise ConfigError("max_write must be 1, 2, 4 or 8")
        if not self.terminator:
            raise ConfigError("terminator must not be empty")
        if self.timeout_ms <= 0:
            raise ConfigError("timeout_ms must be positive")
        if self.retries < 0:
            raise ConfigError("retries must be 0 or more")
        if self.reply_regex and self.data_format == "ieee_block":
            raise ConfigError("reply_regex cannot be combined with data_format = ieee_block")
        self._regex = re.compile(self.reply_regex) if self.reply_regex else None
        for key, allowed in TEMPLATE_FIELDS.items():
            names = {n for _, n, _, _ in string.Formatter().parse(getattr(self, key)) if n is not None}
            if names - allowed:
                raise ConfigError(f"{key} template uses unknown placeholders: {', '.join(sorted(names - allowed))}")
            if key == "read" and not {"addr", "len"} <= names:
                raise ConfigError("read template needs {addr} and {len}")
            if key == "write" and not {"addr", "value"} <= names:
                raise ConfigError("write template needs {addr} and {value}")
        # Catch format typos now rather than on the first command.
        self.format_read(0, 0, 1)
        self.format_write(0, 0, 0, 1)

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
        unknown = set(table) - known
        if unknown:
            raise ConfigError(f"unknown [scpi] keys: {', '.join(sorted(unknown))}")
        return cls(**table, ranges=[Range.from_dict(r) for r in ranges or []])

    # --- commands ----------------------------------------------------------------------------

    def format_read(self, mcu: int, addr: int, length: int) -> str:
        return self._fill(self.read, mcu=mcu, addr=addr, len=length)

    def format_write(self, mcu: int, addr: int, value: int, size: int) -> str:
        return self._fill(self.write, mcu=mcu, addr=addr, value=value, size=size)

    def _fill(self, template: str, **kw) -> str:
        try:
            return fill_template(template, self.addr_format, self.value_format, **kw)
        except (ValueError, KeyError, IndexError) as e:
            raise ConfigError(f"cannot fill {template!r}: {e}") from None

    # --- replies -----------------------------------------------------------------------------

    def decode_data(self, reply: bytes, length: int) -> bytes:
        """Decode a read reply (without its terminator) into exactly ``length`` bytes."""
        if self.data_format == "ieee_block":
            data = parse_ieee_block(reply)
        else:
            text = reply.decode("latin-1").strip()
            if self._regex:
                m = self._regex.search(text)
                if not m:
                    raise BadReply(f"reply {text[:60]!r} does not match reply_regex")
                text = m.group("data") if "data" in self._regex.groupindex else m.group(1 if m.groups() else 0)
            data = decode_text(text, self.data_format)
        if len(data) != length:
            raise BadReply(f"expected {length} bytes, got {len(data)}")
        return data


def decode_text(text: str, fmt: str) -> bytes:
    text = text.strip()
    try:
        if fmt == "hex":
            t = "".join(text.split())
            if t[:2].lower() == "0x":
                t = t[2:]
            return bytes.fromhex(t)
        parts = [p.strip() for p in text.split(",")] if text else []
        if fmt == "bytes_csv":
            values = [int(p[2:] if p[:2].lower() == "0x" else p, 16) for p in parts]
        else:
            values = [int(p, 10) for p in parts]
        return bytes(values)
    except ValueError:
        raise BadReply(f"reply {text[:60]!r} is not valid {fmt} data") from None


def parse_ieee_block(reply: bytes) -> bytes:
    """``#<n><length digits><data>``, the IEEE 488.2 definite-length block."""
    if len(reply) < 2 or reply[:1] != b"#" or not reply[1:2].isdigit() or reply[1:2] == b"0":
        raise BadReply(f"reply {reply[:20]!r} is not an IEEE 488.2 block")
    n = int(reply[1:2])
    digits = reply[2:2 + n]
    if len(digits) != n or not digits.isdigit():
        raise BadReply("IEEE 488.2 block header is truncated")
    length = int(digits)
    data = reply[2 + n:2 + n + length]
    if len(data) != length:
        raise BadReply(f"IEEE 488.2 block is {len(data)} bytes, header says {length}")
    return data


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
