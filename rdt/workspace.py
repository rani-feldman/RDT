"""Workspace (``.rdtw``): everything about one bench in one TOML file.

A workspace is a target file (connection, SCPI profile, MCUs) plus what the user set up on top
of it: the watch tabs, each a named watch list, with each item's display format, scaling,
polling and write safety, and the chart layout. A plain target file loads as a workspace with no
tabs, and a workspace works anywhere a target file does.

An expression may be in several tabs. It is still one item: its settings are the same in every
tab, and it is read once per poll.

    ws = Workspace.load("bench.rdtw")
    with Target(ws.config) as t:
        for w in ws.watches:
            print(ws.read(t, w))

Saving rewrites the whole file, so comments in it are not kept. Paths (ELFs, a profile file)
are saved relative to the workspace.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable

from . import scheduler, tomlw
from .console import SavedCommand
from .errors import ConfigError, RdtError, SymbolError, WriteRefused
from .profile import Profile, Range
from .scaling import Display
from .symbols import Location, parse
from .symbols.expr import expr_text
from .symbols.types import BaseType, is_scalar
from .symbols.values import EnumValue, format_scalar
from .target import Target, TargetConfig

POLL_KINDS = ("global", "manual")
DEFAULT_POLL_MS = 500


def canonical(expr: str) -> str:
    """The expression written the one canonical way, so ``main: motor . speed`` matches."""
    try:
        return expr_text(parse(expr))
    except SymbolError:
        return expr.strip()


@dataclass
class Watch:
    """One watched item: a variable or raw address, and how to show and guard it."""

    expr: str
    display: Display = field(default_factory=Display)
    poll: str | int = "global"     # global | manual | a time in ms
    read_only: bool = False
    min: float | None = None       # write limits, in shown (scaled) units
    max: float | None = None
    confirm: bool = False          # ask before writing

    def __post_init__(self) -> None:
        self.expr = canonical(self.expr)
        if isinstance(self.poll, str) and self.poll not in POLL_KINDS:
            raise ConfigError(f"{self.expr}: poll must be global, manual or a time in ms, not {self.poll!r}")
        if isinstance(self.poll, int) and self.poll < 1:
            raise ConfigError(f"{self.expr}: poll must be at least 1 ms")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ConfigError(f"{self.expr}: min is above max")

    def interval_ms(self, global_ms: int) -> int | None:
        """How often to poll it; None when it is polled only by hand (or polling is off)."""
        if self.poll == "manual":
            return None
        if self.poll == "global":
            return global_ms or None
        return int(self.poll)

    def to_dict(self) -> dict:
        d: dict = {"expr": self.expr}
        if self.poll != "global":
            d["poll"] = self.poll
        d.update(self.display.to_dict())
        if self.read_only:
            d["read_only"] = True
        if self.min is not None:
            d["min"] = _num(self.min)
        if self.max is not None:
            d["max"] = _num(self.max)
        if self.confirm:
            d["confirm"] = True
        return d

    @classmethod
    def from_dict(cls, t: dict) -> Watch:
        if "expr" not in t:
            raise ConfigError(f"every [[watch]] needs an expr, such as expr = \"main:motor.speed\": {t!r}")
        known = {"expr", "poll", "format", "q", "scale", "offset", "unit", "read_only", "min", "max", "confirm"}
        unknown = set(t) - known
        if unknown:
            raise ConfigError(f"[[watch]] {t['expr']}: unknown keys {', '.join(sorted(unknown))}")
        poll = t.get("poll", "global")
        try:
            return cls(str(t["expr"]), Display.from_dict(t), poll if isinstance(poll, str) else int(poll),
                       bool(t.get("read_only", False)), _opt_float(t.get("min")), _opt_float(t.get("max")),
                       bool(t.get("confirm", False)))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"[[watch]] {t['expr']}: {e}") from None


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


@dataclass
class Tab:
    """One watch tab: a name and its own list of watched items."""

    name: str
    watches: list[Watch] = field(default_factory=list)

    def watch(self, expr: str) -> Watch | None:
        key = canonical(expr)
        return next((w for w in self.watches if w.expr == key), None)


DEFAULT_TAB = "Watch"


@dataclass
class Shown:
    """One value as the workspace shows it: a leaf of a watched item."""

    path: str
    text: str | None  # None for a struct or array header row (only with ``headers=True``)
    value: object     # the shown value: scaled number, raw int, bool, EnumValue, bytes ...
    raw: object       # the value as read, before scaling
    depth: int = 0    # 0 for the watched item itself, 1 for its fields and cells, and so on
    type_name: str = ""
    addr: int = 0
    scalar: bool = True   # a value that can be written


@dataclass
class Reading:
    """One watched item from one poll: its values as shown, or why there are none."""

    watch: Watch
    time: float                       # the PC's clock (time.time()) when the reply arrived
    loc: Location | None = None
    shown: list[Shown] | None = None
    error: Exception | None = None


@dataclass
class Workspace:
    config: TargetConfig = field(default_factory=TargetConfig)
    tabs: list[Tab] = field(default_factory=list)
    poll_ms: int = DEFAULT_POLL_MS   # the global polling interval; 0 is off
    chart: dict = field(default_factory=dict)   # owned by the GUI's chart; kept as written
    commands: list[SavedCommand] = field(default_factory=list)   # the device's known commands (CON-7)
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)   # about the file as it was loaded

    # --- files -------------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Workspace:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except OSError as e:
            raise ConfigError(f"cannot read workspace {path}: {e.strerror}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"workspace {path}: {e}") from None
        ws = cls.from_dict(raw, path.parent)
        ws.path = path
        return ws

    @classmethod
    def from_dict(cls, raw: dict, base: Path = Path(".")) -> Workspace:
        poll = raw.get("poll_ms", DEFAULT_POLL_MS)
        if poll == "off":
            poll = 0
        if not isinstance(poll, int) or poll < 0:
            raise ConfigError("poll_ms must be a time in ms, or 0 (or \"off\") for no polling")
        tabs = _tabs_from(raw)
        chart = raw.get("chart", {})
        if not isinstance(chart, dict):
            raise ConfigError("[chart] must be a table")
        ws = cls(TargetConfig.from_dict(raw, base), tabs, poll, chart, _commands_from(raw))
        if raw.get("button"):
            ws.warnings.append("command buttons are no longer supported: the [[button]] tables are ignored "
                               "and are left out when the workspace is saved")
        return ws

    def to_dict(self, base: Path) -> dict:
        cfg = self.config
        d: dict = {}
        if cfg.profile_path is not None:
            d["profile"] = _rel(cfg.profile_path, base)
        d["poll_ms"] = self.poll_ms
        c = cfg.connection
        conn: dict = {"transport": c.transport}
        if c.transport == "serial":
            conn["port"] = c.address
            conn["baud"] = c.baud
        else:
            conn["address"] = c.address
        d["connection"] = conn
        if cfg.profile_path is None:
            d["scpi"] = _profile_dict(cfg.profile)
            if cfg.profile.ranges:
                d["ranges"] = [_range_dict(r) for r in cfg.profile.ranges]
        mcus = []
        for m in cfg.mcus:
            md: dict = {"id": m.id, "name": m.name}
            if m.elf is not None:
                md["elf"] = _rel(m.elf, base)
            if m.timeout_ms is not None:
                md["timeout_ms"] = _num(m.timeout_ms)
            if m.byte_order:
                md["byte_order"] = m.byte_order
            if m.ranges:
                md["ranges"] = [_range_dict(r) for r in m.ranges]
            mcus.append(md)
        if mcus:
            d["mcu"] = mcus
        tabs, written = [], set()
        for tab in self.tabs:
            td: dict = {"name": tab.name}
            if tab.watches:
                # an item in several tabs keeps its settings at its first place only
                td["watch"] = [w.to_dict() if w.expr not in written else {"expr": w.expr} for w in tab.watches]
                written.update(w.expr for w in tab.watches)
            tabs.append(td)
        if tabs:
            d["tab"] = tabs
        if self.chart:
            d["chart"] = self.chart
        if self.commands:
            d["command"] = [{k: v for k, v in (("command", c.command), ("name", c.name), ("group", c.group),
                                                ("description", c.description)) if v} for c in self.commands]
        return d

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path is not None else self.path
        if path is None:
            raise ConfigError("the workspace has no file yet; give a path to save it to")
        text = tomlw.dumps(self.to_dict(path.parent), "RDT workspace. Saved by RDT, so comments are not kept.")
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            raise ConfigError(f"cannot save workspace {path}: {e.strerror}") from None
        self.path = path
        return path

    # --- tabs ---------------------------------------------------------------------------------

    def tab(self, name: str) -> Tab:
        """The tab with this name (any case)."""
        for t in self.tabs:
            if t.name.lower() == name.strip().lower():
                return t
        names = ", ".join(t.name for t in self.tabs) or "none"
        raise ConfigError(f"no tab {name!r}; the workspace has {names}")

    def has_tab(self, name: str) -> bool:
        return any(t.name.lower() == name.strip().lower() for t in self.tabs)

    def add_tab(self, name: str, index: int | None = None) -> Tab:
        name = _tab_name(name)
        if self.has_tab(name):
            raise ConfigError(f"there is already a tab {name!r}")
        tab = Tab(name)
        self.tabs.insert(len(self.tabs) if index is None else index, tab)
        return tab

    def remove_tab(self, name: str) -> Tab:
        tab = self.tab(name)
        self.tabs.remove(tab)
        return tab

    def rename_tab(self, old: str, new: str) -> Tab:
        tab = self.tab(old)
        new = _tab_name(new)
        if new.lower() != tab.name.lower() and self.has_tab(new):
            raise ConfigError(f"there is already a tab {new!r}")
        tab.name = new
        return tab

    def ensure_tab(self) -> Tab:
        """The first tab, made (as "Watch") when the workspace has none."""
        if not self.tabs:
            self.tabs.append(Tab(DEFAULT_TAB))
        return self.tabs[0]

    def tabs_with(self, expr: str) -> list[Tab]:
        key = canonical(expr)
        return [t for t in self.tabs if t.watch(key)]

    # --- the watch list ----------------------------------------------------------------------

    @property
    def watches(self) -> list[Watch]:
        """Every watched item once, in tab order."""
        out, seen = [], set()
        for t in self.tabs:
            for w in t.watches:
                if w.expr not in seen:
                    seen.add(w.expr)
                    out.append(w)
        return out

    def watch(self, expr: str) -> Watch | None:
        """The watched item for exactly this expression, from whichever tab has it."""
        key = canonical(expr)
        for t in self.tabs:
            w = t.watch(key)
            if w is not None:
                return w
        return None

    def guard_for(self, expr: str) -> tuple[Watch | None, Watch | None]:
        """The item for this expression, and the watched struct or array it is inside, if any.
        A read-only or confirm-first struct makes its fields read-only or confirm-first too."""
        key = canonical(expr)
        exact = self.watch(key)
        parent = None
        for w in self.watches:
            if w.expr != key and key.startswith(w.expr) and key[len(w.expr):][:1] in (".", "[", "-"):
                if parent is None or len(w.expr) > len(parent.expr):
                    parent = w
        return exact, parent

    def add(self, w: Watch, tab: str | None = None) -> Watch:
        """Add an item to a tab (the first one by default). An item already in another tab is
        shared: the tab gets that item, with its settings."""
        t = self.tab(tab) if tab is not None else self.ensure_tab()
        if t.watch(w.expr):
            raise ConfigError(f"{w.expr} is already in tab {t.name!r}")
        w = self.watch(w.expr) or w
        t.watches.append(w)
        return w

    def remove(self, expr: str, tab: str | None = None) -> Watch:
        """Remove an item from one tab, or from every tab when none is named."""
        key = canonical(expr)
        tabs = [self.tab(tab)] if tab is not None else self.tabs
        found = None
        for t in tabs:
            w = t.watch(key)
            if w is not None:
                t.watches.remove(w)
                found = w
        if found is None:
            where = f"tab {tabs[0].name!r}" if tab is not None else "the watch list"
            raise ConfigError(f"{key} is not in {where}")
        return found

    def replace(self, new: Watch) -> None:
        """Give an item new settings, in every tab that has it."""
        found = False
        for t in self.tabs:
            for i, w in enumerate(t.watches):
                if w.expr == new.expr:
                    t.watches[i] = new
                    found = True
        if not found:
            raise ConfigError(f"{new.expr} is not in the watch list")

    # --- values ------------------------------------------------------------------------------

    def read(self, t: Target, item: Watch | str, max_cells: int = 100, compact: bool = False,
             headers: bool = False) -> tuple[Location, list[Shown]]:
        """Read a watched item (or any expression) and show it with its format and scaling.
        ``compact`` drops the extras for narrow columns: no hex beside decimals, enum names alone.
        ``headers`` also returns a row for each struct and array, as a tree shows them."""
        w = self._item(item)
        loc, rows = t.read_rows(w.expr, max_cells)
        return loc, self.show(w, loc, rows, compact, headers)

    def read_many(self, t: Target, items: list[Watch | str], max_cells: int | dict[str, int] = 100,
                  compact: bool = False, headers: bool = False) -> list[Reading]:
        """Read several items at once through the poll scheduler: nearby items share one read.
        ``max_cells`` may give each item's own limit, by expression. Errors are kept per item."""
        watches = [self._item(i) for i in items]
        out = []
        for w, s in zip(watches, scheduler.read_many(t, [w.expr for w in watches])):
            r = Reading(w, s.time, s.loc, error=s.error)
            if s.error is None:
                cells = max_cells.get(w.expr, 100) if isinstance(max_cells, dict) else max_cells
                try:
                    r.shown = self.show(w, s.loc, t.rows(s.loc, s.data, cells), compact, headers)
                except RdtError as e:
                    r.error = e
            out.append(r)
        return out

    def _item(self, item: Watch | str) -> Watch:
        return item if isinstance(item, Watch) else (self.watch(item) or Watch(item))

    def show(self, w: Watch, loc: Location, rows: list, compact: bool = False, headers: bool = False) -> list[Shown]:
        """Rows of a value already read, with the item's format and scaling."""
        if w.display.scaled and not is_scalar(loc.type) and loc.bit_size is None:
            raise ConfigError(f"{w.expr}: scaling needs a single value, not a {loc.type.name}")
        out = []
        for r in rows:
            extra = dict(depth=r.depth, type_name=r.type_name, addr=r.addr,
                         scalar=r.value is not None and not isinstance(r.value, bytes))
            if r.text is None:
                if headers:
                    out.append(Shown(r.path, None, None, None, **{**extra, "scalar": False}))
                continue
            if r.value is None or isinstance(r.value, (bytes, EnumValue, bool)) or not _is_number(r.type):
                text = r.text
                if compact and r.value is not None and not isinstance(r.value, bytes):
                    text = format_scalar(r.type, r.value, r.bit_size, hex_too=False)
                out.append(Shown(r.path, _with_unit(text, w.display), r.value, r.value, **extra))
                continue
            shown = w.display.to_shown(r.value)
            bits = r.bit_size if r.bit_size is not None else 8 * r.type.size
            if not w.display.scaled and w.display.format == "dec":
                if isinstance(shown, float) or compact:
                    text = w.display.text(shown)
                else:
                    text = _with_unit(r.text, w.display)
            else:
                text = w.display.text(shown, bits)
            out.append(Shown(r.path, text, shown, r.value, **extra))
        return out

    def write(self, t: Target, expr: str, text: str,
              confirm: Callable[[str], bool] | None = None) -> tuple[Location, object]:
        """Write a value typed in shown units, through the write safety. Returns the location and
        the raw value written. ``confirm`` is asked when the item wants confirming; without it,
        such a write is refused."""
        exact, parent = self.guard_for(expr)
        guard = exact or Watch(expr)
        name = canonical(expr)
        if guard.read_only or (parent and parent.read_only):
            raise WriteRefused(f"{name} is read-only in the workspace" +
                               ("" if guard.read_only else f" (it is part of {parent.expr})"))
        loc = t.resolve(name)
        if not is_scalar(loc.type):
            raise SymbolError(f"{loc.path} is a {loc.type.name}; write one field or cell at a time")
        is_float = isinstance(loc.type.strip(), BaseType) and loc.type.strip().kind == "float" and loc.bit_size is None
        if guard.display.scaled:
            try:
                shown = float(text)
            except ValueError:
                raise ConfigError(f"{name}: {text!r} is not a number") from None
            raw = guard.display.to_raw(shown, is_float)
            check = shown
        else:
            raw = t.parse_value(loc, text)
            check = raw
        if guard.min is not None or guard.max is not None:
            if isinstance(check, (bytes, str)) or check is None:
                raise WriteRefused(f"{name}: limits need a number")
            if guard.min is not None and check < guard.min:
                raise WriteRefused(f"{name}: {text} is below its minimum {guard.min:g}")
            if guard.max is not None and check > guard.max:
                raise WriteRefused(f"{name}: {text} is above its maximum {guard.max:g}")
        if guard.confirm or (parent and parent.confirm):
            question = f"Write {text}{' ' + guard.display.unit if guard.display.unit else ''} to {name}?"
            if confirm is None or not confirm(question):
                raise WriteRefused(f"{name}: write not confirmed")
        if guard.display.scaled and not isinstance(raw, float):
            raw = t.parse_value(loc, str(raw))   # checks that the raw value fits the type
        t.write_value(loc, raw)
        return loc, raw


def _tab_name(name: str) -> str:
    name = str(name).strip()
    if not name:
        raise ConfigError("a tab needs a name")
    return name


def _tabs_from(raw: dict) -> list[Tab]:
    """The tabs of a workspace file. A file from before tabs has one plain [[watch]] list; it
    becomes the tab "Watch"."""
    tabs: list[Tab] = []
    shared: dict[str, Watch] = {}
    names = set()

    def take(tab: Tab, items) -> None:
        for t in items:
            w = Watch.from_dict(t)
            if tab.watch(w.expr):
                raise ConfigError(f"{w.expr} is in tab {tab.name!r} twice")
            first = shared.get(w.expr)
            if first is None:
                shared[w.expr] = w
            elif set(t) != {"expr"} and w != first:
                raise ConfigError(f"{w.expr} has different settings in two tabs; an item has one set of "
                                  f"settings, so give them only at its first place")
            else:
                w = first
            tab.watches.append(w)

    plain = raw.get("watch", [])
    if plain:
        tab = Tab(DEFAULT_TAB)
        take(tab, plain)
        tabs.append(tab)
        names.add(tab.name.lower())
    for t in raw.get("tab", []):
        if not isinstance(t, dict):
            raise ConfigError("every [[tab]] must be a table with a name")
        unknown = set(t) - {"name", "watch"}
        if unknown:
            raise ConfigError(f"[[tab]] {t.get('name', '')!r}: unknown keys {', '.join(sorted(unknown))}")
        name = _tab_name(t.get("name", ""))
        if name.lower() in names:
            if plain and name.lower() == DEFAULT_TAB.lower():
                tab = tabs[0]   # the old plain list and a [[tab]] "Watch" are the same tab
                take(tab, t.get("watch", []))
                continue
            raise ConfigError(f"there are two tabs named {name!r}")
        names.add(name.lower())
        tab = Tab(name)
        take(tab, t.get("watch", []))
        tabs.append(tab)
    return tabs


def _commands_from(raw: dict) -> list[SavedCommand]:
    out = []
    for t in raw.get("command", []):
        if not isinstance(t, dict) or not isinstance(t.get("command"), str) or not t["command"].strip():
            raise ConfigError("every [[command]] must be a table with a command")
        unknown = set(t) - {"command", "name", "group", "description"}
        if unknown:
            raise ConfigError(f"[[command]] {t['command']!r}: unknown keys {', '.join(sorted(unknown))}")
        out.append(SavedCommand(t["command"].strip(), *(str(t.get(k, "")) for k in ("name", "group", "description"))))
    return out


def _num(v: float) -> float | int:
    """A float that is a whole number, written as an int: 1000, not 1000.0."""
    return int(v) if isinstance(v, float) and float(v).is_integer() else v


def _is_number(t) -> bool:
    s = t.strip()
    return isinstance(s, BaseType) and s.kind != "bool"


def _with_unit(text: str, d: Display) -> str:
    return f"{text} {d.unit}" if d.unit else text


def _rel(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:   # another drive on Windows
        return Path(path).as_posix()


def _range_dict(r: Range) -> dict:
    d: dict = {"name": r.name, "start": tomlw.Hex(r.start), "size": tomlw.Hex(r.size)}
    if not r.writable:
        d["writable"] = False
    if r.mcu not in (None, ""):
        d["mcu"] = r.mcu
    return d


def _profile_dict(p: Profile) -> dict:
    default = Profile()
    d = {}
    for f in fields(Profile):
        if f.name == "ranges":
            continue
        v = getattr(p, f.name)
        if v != getattr(default, f.name) or f.name == "name":
            d[f.name] = "" if v is None else _num(v)
    return d
