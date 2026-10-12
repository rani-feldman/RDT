"""SCPI profile: the user-defined command templates and reply format.

Nothing about the firmware's commands is hard-coded. The profile says how to spell a read, a
write and the error query. Addresses and values in commands, and the data in read replies, are
always bare hex with no prefix: address 0x11AABBCCDD is sent as ``11AABBCCDD`` (rani,
2026-10-12). The simulator reads the same ``[scpi]`` table, so one file describes both sides.
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
RETIRED_KEYS = {"size_query", "addr_format", "value_format", "data_format"}
_HEX_SPEC = re.compile(r"^0?\d*X$")
TEMPLATE_FIELDS = {"read": {"mcu", "addr", "len"}, "write": {"mcu", "addr", "value", "size"}}


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


def fill_template(template: str, **values) -> str:
    """Fill a command template. ``{addr}`` and ``{value}`` are written as bare upper-case hex with
    no prefix; a template may pad them, as in ``{addr:08X}``, but not change the base."""
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
                                  f"bare hex; only a width such as {{{name}:08X}} is allowed")
            out.append(format(value, spec or "X"))
        elif spec:
            out.append(format(value, spec))
        else:
            out.append(str(value))
    return "".join(out)


@dataclass
class Profile:
    name: str = "Default"
    read: str = "MEMory:READ? {mcu},{addr},{len}"
    write: str = "MEMory:WRITE {mcu},{addr},{value},{size}"
    max_write: int = 4                     # largest value one write accepts, in bytes
    reply_regex: str | None = None         # optional; group "data" (or group 1) holds the hex data
    byte_order: str = "little"             # used for an MCU without an ELF
    max_response: int = 1024               # bytes of data one read reply can carry; set here only
    error_query: str | None = "SYSTem:ERRor?"
    probe: str | None = "*IDN?"            # sent on connect and reconnect to check the device answers
    terminator: str = "\n"
    timeout_ms: float = 500
    retries: int = 2                       # extra attempts after a read times out
    ranges: list[Range] = field(default_factory=list)

    def __post_init__(self) -> None:
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
        table = {k: v for k, v in table.items() if k not in RETIRED_KEYS}
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
            return fill_template(template, **kw)
        except (ValueError, KeyError, IndexError) as e:
            raise ConfigError(f"cannot fill {template!r}: {e}") from None

    # --- replies -----------------------------------------------------------------------------

    def decode_data(self, reply: bytes, length: int) -> bytes:
        """Decode a read reply (without its terminator) into exactly ``length`` bytes.

        The reply is the bytes in bare hex, lowest address first, such as ``0000C03F``.
        """
        text = reply.decode("latin-1").strip()
        if self._regex:
            m = self._regex.search(text)
            if not m:
                raise BadReply(f"reply {text[:60]!r} does not match reply_regex")
            text = m.group("data") if "data" in self._regex.groupindex else m.group(1 if m.groups() else 0)
        data = decode_hex(text)
        if len(data) != length:
            raise BadReply(f"expected {length} bytes, got {len(data)}")
        return data


_HEX_DATA = re.compile(r"^[0-9A-Fa-f\s]*$")


def decode_hex(text: str) -> bytes:
    """Bare hex bytes, such as ``0000C03F``. A prefix such as ``0x`` is not accepted."""
    t = "".join(text.split())
    if not _HEX_DATA.match(t) or len(t) % 2:
        raise BadReply(f"reply {text[:60]!r} is not bare hex bytes")
    return bytes.fromhex(t)


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
