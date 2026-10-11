"""Target API: the one entry point for the CLI, the GUI and Python scripts.

A target is a device reached over one link, with one or more MCUs. Each MCU has a name, an id
and usually its own ELF. Every access names its MCU, always: there is no default MCU.

    with Target.from_file("bench.toml") as t:
        t.read_value("@main:0x20000010:u32")
        t.write_value("@motorctl:0x20000084:u32", 750)

The target checks every access against the MCU's memory map before anything is sent, then
hands it to the session, which deals with timeouts, retries and confirming writes.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import codec
from .errors import AccessRefused, ConfigError, DeviceError
from .memmap import ElfInfo, MemoryMap, load_elf_sections
from .profile import Profile, Range, parse_int
from .session import LinkState, Session
from .transport import KINDS, Transport, make_transport


@dataclass
class ConnectionConfig:
    transport: str = "tcp"         # tcp | udp | serial
    address: str = "127.0.0.1:5025"  # HOST:PORT, or the COM port name for serial
    baud: int = 115200

    def describe(self) -> str:
        return f"{self.transport} {self.address}" + (f" {self.baud}" if self.transport == "serial" else "")


@dataclass
class McuConfig:
    id: int
    name: str
    elf: Path | None = None
    timeout_ms: float | None = None   # overrides the profile's timeout for this MCU
    byte_order: str | None = None     # default: from the ELF, else the profile's
    ranges: list[Range] = field(default_factory=list)


@dataclass
class TargetConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    profile: Profile = field(default_factory=Profile)
    mcus: list[McuConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> TargetConfig:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except OSError as e:
            raise ConfigError(f"cannot read target file {path}: {e.strerror}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"target file {path}: {e}") from None
        return cls.from_dict(raw, path.parent)

    @classmethod
    def from_dict(cls, raw: dict, base: Path = Path(".")) -> TargetConfig:
        conn = raw.get("connection", {})
        kind = conn.get("transport", "tcp")
        if kind == "serial":
            address = conn.get("port", "")
            if not address:
                raise ConfigError("[connection] transport = \"serial\" needs port, such as \"COM12\"")
        else:
            address = conn.get("address") or f"{conn.get('host', '127.0.0.1')}:{conn.get('port', 5025)}"
        connection = ConnectionConfig(kind, str(address), int(conn.get("baud", 115200)))
        if kind not in KINDS:
            raise ConfigError(f"[connection] transport must be one of {', '.join(KINDS)}")

        if isinstance(raw.get("profile"), str):
            profile = Profile.load(base / raw["profile"])
        else:
            profile = Profile.from_dict(raw.get("scpi", {}), raw.get("ranges", []))

        mcus = []
        for t in raw.get("mcu", []):
            if "id" not in t or "name" not in t:
                raise ConfigError("every [[mcu]] needs an id and a name")
            mcus.append(McuConfig(
                id=int(t["id"]),
                name=str(t["name"]),
                elf=(base / t["elf"]) if t.get("elf") else None,
                timeout_ms=float(t["timeout_ms"]) if "timeout_ms" in t else None,
                byte_order=t.get("byte_order"),
                ranges=[Range.from_dict(r) for r in t.get("ranges", [])],
            ))
        return cls(connection, profile, mcus)


class Mcu:
    """One MCU of the target: its id, name, byte order and memory map."""

    def __init__(self, cfg: McuConfig, profile: Profile):
        self.id = cfg.id
        self.name = cfg.name
        self.elf: ElfInfo | None = load_elf_sections(cfg.elf) if cfg.elf else None
        self.byte_order = cfg.byte_order or (self.elf.byte_order if self.elf else profile.byte_order)
        if self.byte_order not in ("little", "big"):
            raise ConfigError(f"MCU {self.name}: byte_order must be little or big")
        self.timeout_ms = cfg.timeout_ms
        shared = [r for r in profile.ranges if r.mcu in (None, "", cfg.name, cfg.id)]
        self.memory = MemoryMap(self.elf.sections if self.elf else [], shared + cfg.ranges)

    def __repr__(self) -> str:
        return f"Mcu({self.id}, {self.name!r})"


_NAME = re.compile(r"^[A-Za-z_][\w.-]*$")


@dataclass(frozen=True)
class RawRef:
    """A raw address on one MCU, as in ``@main:0x20000418:u32``."""

    mcu: str
    addr: int
    type: str

    @property
    def size(self) -> int:
        return codec.size_of(self.type)

    def __str__(self) -> str:
        return f"@{self.mcu}:0x{self.addr:08X}:{self.type}"


def parse_ref(text: str) -> RawRef:
    """Parse ``@mcu:address:type``. Symbol names such as ``main:motor.speed`` come with M3."""
    t = text.strip()
    if not t.startswith("@"):
        raise ConfigError(f"{text!r}: expected @mcu:address:type, such as @main:0x20000010:u32 "
                          f"(variable names come with ELF symbol support)")
    parts = t[1:].split(":")
    if len(parts) != 3 or not parts[0]:
        raise ConfigError(f"{text!r}: expected @mcu:address:type, such as @main:0x20000010:u32")
    mcu, addr, type_name = parts
    try:
        address = parse_int(addr)
    except ValueError:
        raise ConfigError(f"{text!r}: {addr!r} is not an address") from None
    if type_name not in codec.TYPES:
        raise ConfigError(f"{text!r}: unknown type {type_name!r}; use one of {', '.join(codec.TYPES)}")
    return RawRef(mcu, address, type_name)


class Target:
    def __init__(self, config: TargetConfig, transport: Transport | None = None, *, auto_reconnect: bool = True):
        if not config.mcus:
            raise ConfigError("the target has no MCUs; add at least [[mcu]] id = 0")
        ids = [m.id for m in config.mcus]
        names = [m.name for m in config.mcus]
        if 0 not in ids:
            raise ConfigError("MCU 0, the main MCU, is required")
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ConfigError("MCU ids and names must be unique")
        for n in names:
            if not _NAME.match(n):
                raise ConfigError(f"MCU name {n!r} must start with a letter and hold no spaces or colons")
        self.config = config
        self.profile = config.profile
        self.mcus = {m.name: Mcu(m, config.profile) for m in sorted(config.mcus, key=lambda m: m.id)}
        c = config.connection
        self.transport = transport or make_transport(c.transport, c.address, c.baud)
        timeouts = {m.id: m.timeout_ms for m in self.mcus.values() if m.timeout_ms}
        names = {m.id: m.name for m in self.mcus.values()}
        self.session = Session(self.transport, self.profile, mcu_timeouts_ms=timeouts, mcu_names=names,
                               auto_reconnect=auto_reconnect)

    @classmethod
    def from_file(cls, path: str | Path, **kw) -> Target:
        return cls(TargetConfig.load(path), **kw)

    # --- link --------------------------------------------------------------------------------

    def connect(self) -> Target:
        self.session.connect()
        return self

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Target:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def link_state(self) -> LinkState:
        return self.session.state

    @property
    def idn(self) -> str | None:
        return self.session.idn

    @property
    def max_response(self) -> int:
        return self.session.max_response

    def is_silent(self, mcu: str | Mcu) -> bool:
        return self.session.is_silent(self.mcu(mcu).id)

    # --- MCUs --------------------------------------------------------------------------------

    def mcu(self, name: str | Mcu) -> Mcu:
        if isinstance(name, Mcu):
            return name
        try:
            return self.mcus[name]
        except KeyError:
            raise ConfigError(f"unknown MCU {name!r}; this target has {', '.join(self.mcus)}") from None

    # --- memory ------------------------------------------------------------------------------

    def read(self, mcu: str | Mcu, addr: int, size: int) -> bytes:
        m = self.mcu(mcu)
        self._check(m, addr, size, write=False)
        return self.session.read(m.id, addr, size)

    def write(self, mcu: str | Mcu, addr: int, data: bytes) -> None:
        """Write raw bytes and confirm them by reading them back."""
        m = self.mcu(mcu)
        self._check(m, addr, len(data), write=True)
        self.session.write(m.id, addr, data, m.byte_order)

    def _check(self, m: Mcu, addr: int, size: int, write: bool) -> None:
        try:
            m.memory.check(addr, size, write)
        except AccessRefused as e:
            raise AccessRefused(f"MCU {m.name}: {e}") from None

    def read_value(self, ref: str | RawRef) -> int | float:
        r = parse_ref(ref) if isinstance(ref, str) else ref
        m = self.mcu(r.mcu)
        return codec.decode(self.read(m, r.addr, r.size), r.type, m.byte_order)

    def write_value(self, ref: str | RawRef, value: int | float | str) -> None:
        r = parse_ref(ref) if isinstance(ref, str) else ref
        m = self.mcu(r.mcu)
        if isinstance(value, str):
            value = codec.parse_value(value, r.type)
        self.write(m, r.addr, codec.encode(value, r.type, m.byte_order))

    # --- console -----------------------------------------------------------------------------

    def command(self, text: str, expect_reply: bool | None = None, timeout_ms: float | None = None) -> str | None:
        """Send any SCPI command by hand and return its reply, if it is a query and one came."""
        return self.session.command(text, expect_reply, timeout_ms)

    def error_query(self) -> DeviceError | None:
        return self.session.error_query()
