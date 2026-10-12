"""Target API: the one entry point for the CLI, the GUI and Python scripts.

A target is a device reached over one link, with one or more MCUs. Each MCU has a name, an id
and usually its own ELF. Every access names its MCU, always: there is no default MCU.

    with Target.from_file("bench.toml") as t:
        t.read_value("main:motor.speed")
        t.write_value("motorctl:pwm_duty", 750)
        t.read_value("@main:0x20000010:u32")

Names are resolved through the MCU's ELF (see ``rdt.symbols``). The target checks every access
against the MCU's memory map before anything is sent, then hands it to the session, which deals
with timeouts, retries and confirming writes.
"""

from __future__ import annotations

import logging
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AccessRefused, ConfigError, DeviceError, RdtError, SymbolError, WriteRefused
from .memmap import ElfInfo, MemoryMap, build_id_note, elf_bytes, load_elf_sections
from .profile import Profile, Range
from .session import LinkState, Session
from .symbols import Location, Resolver, Row, SymbolTable, describe_address, load_symbols, parse
from .symbols import values
from .symbols.types import is_scalar
from .transport import KINDS, Transport, make_transport

log = logging.getLogger(__name__)


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
    identity: str | None = None       # a constant variable that tells firmware builds apart (SYM-8)


@dataclass
class Identity:
    """The result of checking that an MCU's ELF matches the firmware it runs (SYM-8)."""

    mcu: str
    status: str        # match, mismatch, unchecked (nothing to compare) or unread (no answer)
    detail: str

    def __str__(self) -> str:
        return f"MCU {self.mcu}: {self.detail}"


@dataclass
class TargetConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    profile: Profile = field(default_factory=Profile)
    mcus: list[McuConfig] = field(default_factory=list)
    profile_path: Path | None = None   # where the profile came from, if it is a file of its own

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

        profile_path = None
        if isinstance(raw.get("profile"), str):
            profile_path = base / raw["profile"]
            profile = Profile.load(profile_path)
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
                identity=str(t["identity"]) if t.get("identity") else None,
            ))
        return cls(connection, profile, mcus, profile_path)


class Mcu:
    """One MCU of the target: its id, name, byte order, memory map and symbols."""

    def __init__(self, cfg: McuConfig, profile: Profile):
        self.id = cfg.id
        self.name = cfg.name
        self.timeout_ms = cfg.timeout_ms
        self._cfg = cfg
        self._profile = profile
        self._failed_stamp: tuple[int, int] | None = None
        self.identity: Identity | None = None     # set by Target.check_identity
        self.allow_mismatch = False               # the user chose to write despite a mismatch
        self._load()

    def _load(self) -> None:
        cfg = self._cfg
        self.elf: ElfInfo | None = load_elf_sections(cfg.elf) if cfg.elf else None
        self.byte_order = cfg.byte_order or (self.elf.byte_order if self.elf else self._profile.byte_order)
        if self.byte_order not in ("little", "big"):
            raise ConfigError(f"MCU {self.name}: byte_order must be little or big")
        shared = [r for r in self._profile.ranges if r.mcu in (None, "", cfg.name, cfg.id)]
        self.memory = MemoryMap(self.elf.sections if self.elf else [], shared + cfg.ranges)
        self._symbols: SymbolTable | None = None
        self._stamp = _stamp(cfg.elf) if cfg.elf else None

    @property
    def symbols(self) -> SymbolTable | None:
        """The ELF's variables and types, read on first use (and cached on disk). None without an ELF."""
        if self.elf is None:
            return None
        if self._symbols is None:
            self._symbols = load_symbols(self.elf.path)
        return self._symbols

    def resolver(self, reader=None) -> Resolver:
        return Resolver(self.name, lambda: self.symbols, self.byte_order, reader)

    def elf_changed(self) -> bool:
        """True when the ELF on disk is not the one loaded, and has been still for a moment."""
        if self._cfg.elf is None:
            return False
        now = _stamp(self._cfg.elf)
        if now is None or now in (self._stamp, self._failed_stamp):
            return False
        return time.time() - now[0] / 1e9 > ELF_SETTLE_S   # the linker may still be writing it

    def reload(self) -> bool:
        """Load the ELF again. On failure (a half-written file) keep the old one and return False."""
        old = (self.elf, self.byte_order, self.memory, self._symbols, self._stamp)
        try:
            self._load()
            if self.elf is not None:
                self._symbols = load_symbols(self.elf.path)
        except ConfigError as e:
            log.warning("MCU %s: cannot reload %s yet: %s", self.name, self._cfg.elf, e)
            self.elf, self.byte_order, self.memory, self._symbols, self._stamp = old
            self._failed_stamp = _stamp(self._cfg.elf)   # tried again when the file changes again
            return False
        log.info("MCU %s: reloaded %s", self.name, self._cfg.elf)
        self.identity, self.allow_mismatch = None, False   # the new ELF has not been checked yet
        return True

    def identity_area(self) -> tuple[int, int, str] | None:
        """Where to compare the ELF with the firmware: address, size and what it is. The MCU's
        ``identity`` variable when set, else the GNU build ID when the linker put it in memory."""
        if self.elf is None:
            return None
        if self._cfg.identity:
            loc = self.resolver().resolve(parse(f"{self.name}:{self._cfg.identity}"))
            return loc.addr, loc.size, self._cfg.identity
        note = build_id_note(self.elf.path)
        return (*note, "the GNU build ID") if note else None

    def __repr__(self) -> str:
        return f"Mcu({self.id}, {self.name!r})"


ELF_SETTLE_S = 0.5


def _show_bytes(data: bytes) -> str:
    """Short text for identity bytes: the string when printable, else hex."""
    text = data.rstrip(b"\0")
    if text and all(32 <= b < 127 for b in text):
        return repr(text.decode("ascii"))
    shown = data[:16].hex(" ").upper()
    return shown + (" ..." if len(data) > 16 else "")


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


_NAME = re.compile(r"^[A-Za-z_][\w.-]*$")


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

    def connect(self, check_identity: bool = True) -> Target:
        """Open the link, then check each MCU's ELF against its firmware (SYM-8)."""
        self.session.connect()
        if check_identity:
            self.check_identity()
        return self

    def close(self) -> None:
        self.session.close()

    def reconnect(self) -> Target:
        """Close the link and open it again, checking the device answers (CON-9) and that each
        ELF still matches its firmware (SYM-8), since the firmware may have been flashed."""
        self.session.close()
        self.session.connect()
        self.check_identity()
        return self

    # --- ELF identity (SYM-8) -----------------------------------------------------------------

    def check_identity(self, mcu: str | Mcu | None = None) -> list[Identity]:
        """Compare each MCU's ELF with the firmware it runs: read the identity bytes from the
        device and from the ELF file. A mismatch makes writes to that MCU refused until
        ``allow_mismatch`` is called. A silent MCU is marked ``unread`` and is not blocked."""
        out = []
        for m in [self.mcu(mcu)] if mcu is not None else self.mcus.values():
            m.identity = self._identity(m)
            m.allow_mismatch = False
            out.append(m.identity)
            if m.identity.status == "mismatch":
                log.warning("%s", m.identity)
        return out

    def _identity(self, m: Mcu) -> Identity:
        try:
            area = m.identity_area()
        except RdtError as e:
            return Identity(m.name, "unchecked", f"identity variable: {e}")
        if area is None:
            return Identity(m.name, "unchecked", "no identity variable or build ID to compare")
        addr, size, what = area
        expected = elf_bytes(m.elf.path, addr, size)
        if expected is None:
            return Identity(m.name, "unchecked", f"{what} has no value in the ELF file (use a const variable)")
        try:
            actual = self.read(m, addr, size)
        except RdtError as e:
            return Identity(m.name, "unread", f"{what} could not be read: {e}")
        if actual == expected:
            return Identity(m.name, "match", f"{what} matches {m.elf.path.name}")
        return Identity(m.name, "mismatch", f"{what} differs from {m.elf.path.name}: "
                                            f"device {_show_bytes(actual)}, ELF {_show_bytes(expected)}")

    def allow_mismatch(self, mcu: str | Mcu) -> None:
        """Let writes through to an MCU whose ELF does not match, until the next check."""
        self.mcu(mcu).allow_mismatch = True

    def send_break(self, ms: float = 250) -> None:
        """Send a serial break on a COM port link (CON-10)."""
        self.session.send_break(ms)

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

    @property
    def align(self) -> int:
        """Every read and write covers whole aligned words of this many bytes."""
        return self.session.profile.align

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
        if m.identity is not None and m.identity.status == "mismatch" and not m.allow_mismatch:
            raise WriteRefused(f"MCU {m.name}: writes are refused because its ELF does not match the firmware "
                               f"({m.identity.detail}); load the right ELF, or allow writes anyway")
        self.session.write(m.id, addr, data, m.byte_order)

    def _check(self, m: Mcu, addr: int, size: int, write: bool) -> None:
        try:
            m.memory.check_aligned(addr, size, self.align, write)
        except AccessRefused as e:
            raise AccessRefused(f"MCU {m.name}: {e}") from None

    # --- values by name --------------------------------------------------------------------

    def resolve(self, expr: str | Location) -> Location:
        """Where an expression's value is: ``main:motor.speed``, ``@main:0x20000010:u32``.

        A location that went through a pointer is resolved again, since the pointer may have moved.
        """
        if isinstance(expr, Location):
            if not expr.dynamic:
                return expr
            expr = expr.path
        e = parse(expr)
        m = self.mcu(e.mcu)
        return m.resolver(lambda addr, size: self.read(m, addr, size)).resolve(e)

    def describe(self, expr: str) -> Location:
        """Resolve without the device: following a pointer is refused."""
        e = parse(expr)
        return self.mcu(e.mcu).resolver().resolve(e)

    def read_location(self, loc: Location) -> bytes:
        return self.read(loc.mcu, loc.addr, loc.size)

    def read_value(self, expr: str | Location):
        """Read a value: a number or bool for a scalar, an ``EnumValue`` for an enum, a dict for a
        struct and a list for an array. A struct or array is read in one block."""
        loc = self.resolve(expr)
        data = self.read_location(loc)
        order = self.mcu(loc.mcu).byte_order
        if loc.bit_size is not None:
            return values.decode_scalar(loc.type, data, order, loc.bit_size, loc.bit_shift)
        return values.decode(loc.type, data, order)

    def rows(self, loc: Location, data: bytes, max_cells: int = 100) -> list[Row]:
        """One row per field and cell of a value already read, for display."""
        m = self.mcu(loc.mcu)
        return values.rows(loc.type, data, m.byte_order, loc.path, loc.addr, max_cells,
                           lambda a, typ: self.symbolize(m, a, typ), loc.bit_size, loc.bit_shift)

    def read_rows(self, expr: str | Location, max_cells: int = 100) -> tuple[Location, list[Row]]:
        loc = self.resolve(expr)
        return loc, self.rows(loc, self.read_location(loc), max_cells)

    def symbolize(self, mcu: str | Mcu, addr: int, target=None) -> str | None:
        """``&name.field`` for an address inside a variable of the MCU's ELF."""
        m = self.mcu(mcu)
        if m.elf is None:
            return None
        try:
            return describe_address(m.symbols, addr, target)
        except (ConfigError, SymbolError):
            return None

    def parse_value(self, loc: Location, text: str):
        if not is_scalar(loc.type):
            raise SymbolError(f"{loc.path} is a {loc.type.name}; write one field or cell at a time")
        try:
            return values.parse_scalar(loc.type, text, loc.bit_size)
        except ValueError as e:
            raise ConfigError(f"{loc.path}: {e}") from None

    def write_value(self, expr: str | Location, value) -> Location:
        """Write one scalar (a number, bool, enum name or bit field) and confirm it by read-back.

        A bit field is written by reading the bytes that hold it, changing the field and writing
        them back, so a change the firmware makes to a neighbouring field in between is lost.
        """
        loc = self.resolve(expr)
        if not is_scalar(loc.type):
            raise SymbolError(f"{loc.path} is a {loc.type.name}; write one field or cell at a time")
        if not isinstance(value, str):
            value = repr(value) if isinstance(value, float) else str(int(value))
        value = self.parse_value(loc, value)
        m = self.mcu(loc.mcu)
        old = self.read_location(loc) if loc.bit_size is not None else None
        data = values.encode_scalar(loc.type, value, m.byte_order, loc.size, loc.bit_size, loc.bit_shift, old)
        self.write(m, loc.addr, data)
        return loc

    def remap(self, loc: Location) -> tuple[Location | None, str | None]:
        """Resolve a watched item again by name after its MCU's ELF was reloaded.

        Returns the new location (None if the name is gone) and a note when something changed:
        it moved, its type changed, or it no longer exists. A raw address keeps its address.
        """
        try:
            new = self.describe(loc.path) if not loc.dynamic else loc
        except SymbolError as e:
            return None, f"{loc.path} is gone after the ELF was rebuilt ({e})"
        except RdtError as e:
            return None, f"{loc.path} can't be resolved after the ELF was rebuilt ({e})"
        notes = []
        if (new.addr, new.bit_shift) != (loc.addr, loc.bit_shift) and not loc.dynamic:
            notes.append(f"moved from 0x{loc.addr:08X} to 0x{new.addr:08X}")
        if (new.type_name, new.size) != (loc.type_name, loc.size):
            notes.append(f"is now {new.type_name} ({new.size} bytes), was {loc.type_name} ({loc.size} bytes)")
        return new, (f"{loc.path} " + " and ".join(notes) if notes else None)

    def refresh_elfs(self) -> list[str]:
        """Reload every MCU's ELF that was rebuilt; returns the names of the MCUs reloaded."""
        return [m.name for m in self.mcus.values() if m.elf_changed() and m.reload()]

    # --- console -----------------------------------------------------------------------------

    def command(self, text: str, expect_reply: bool | None = None, timeout_ms: float | None = None) -> str | None:
        """Send any SCPI command by hand and return its reply, if it is a query and one came."""
        return self.session.command(text, expect_reply, timeout_ms)

    def error_query(self) -> DeviceError | None:
        return self.session.error_query()
