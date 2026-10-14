"""Command line: rdt [connection options] COMMAND ...

    rdt -t bench.toml info
    rdt -t bench.toml symbols main motor*
    rdt -t bench.toml ptype main:motor
    rdt -t bench.toml read main:motor motorctl:temperature @main:0x20000010:u32
    rdt -t bench.toml write main:motor.target 2500
    rdt -t bench.toml dump main:rx_buffer
    rdt -t bench.toml send "*IDN?"
    rdt -t bench.toml watch main:motor.speed -i 100
    rdt -t bench.toml console
    rdt history
    rdt -w bench.rdtw commands import fw_commands.csv

Without -t or -w, rdt uses rdt.toml in the current folder when there is one.
Without a target file, give the link and the MCUs on the command line:

    rdt --tcp 192.168.1.20:5025 --profile fw.toml --mcu main=0:main.elf --mcu motorctl=1:motorctl.elf info
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, codec
from .console import (CommandHistory, SavedCommand, SessionLog, completions, merge_commands, read_commands,
                      write_commands)
from .csvlog import CsvLog
from .errors import ConfigError, LinkDown, RdtError, SymbolError
from .profile import Profile, parse_int
from .symbols import Location
from .symbols.types import ArrayType, EnumType, StructType, Type, is_char_array
from .scaling import FORMATS
from .target import ConnectionConfig, McuConfig, Target, TargetConfig
from .workspace import Reading, Shown, Watch, Workspace


def _int(text: str) -> int:
    try:
        return parse_int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None


def _mcu_option(text: str) -> McuConfig:
    """``NAME=ID[:ELF]``. The ELF path may itself contain a colon, as in C:\\fw\\main.elf."""
    name, sep, rest = text.partition("=")
    mcu_id, _, elf = rest.partition(":")
    if not sep or not name or not mcu_id.strip().isdigit():
        raise argparse.ArgumentTypeError(f"{text!r}: expected NAME=ID[:ELF], such as main=0:main.elf")
    return McuConfig(int(mcu_id), name.strip(), Path(elf) if elf else None)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rdt",
        description="Remote Debugger Tool: read and write firmware memory over a COM port or LAN.",
        epilog="Values are named with their MCU first: main:motor.speed, main:gains[2], main:head->next, "
               "main:motor.c:state (a file-static), or a raw address @main:0x20000010:u32. "
               f"Raw types: {', '.join(codec.TYPES)}, or any type from the ELF.",
    )
    ap.add_argument("--version", action="version", version=f"rdt {__version__}")
    g = ap.add_argument_group("target")
    g.add_argument("-w", "--workspace", "-t", "--target", dest="target", type=Path, metavar="FILE",
                   help="workspace (.rdtw) or target file: connection, profile, MCUs and the watch list "
                        "(default: rdt.toml in the current folder, if there is one)")
    link = g.add_mutually_exclusive_group()
    link.add_argument("--tcp", metavar="HOST:PORT", help="connect over LAN TCP")
    link.add_argument("--udp", metavar="HOST:PORT", help="connect over LAN UDP")
    link.add_argument("--serial", metavar="PORT", help="connect over a COM port, such as COM12")
    g.add_argument("--baud", type=int, help="COM port baud rate (default 115200)")
    g.add_argument("--profile", type=Path, help="SCPI profile (TOML with an [scpi] table)")
    g.add_argument("--mcu", type=_mcu_option, action="append", metavar="NAME=ID[:ELF]",
                   help="an MCU of the device; repeat for each. Replaces the target file's MCUs")
    g.add_argument("--timeout", type=float, metavar="MS", help="reply timeout in ms, for every MCU")
    g.add_argument("--allow-mismatch", action="store_true",
                   help="write even to an MCU whose ELF does not match its firmware")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v: link events, -vv: every command and reply")

    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    sub.add_parser("info", help="identify the device and list its MCUs and memory")

    p = sub.add_parser("symbols", help="list or search an MCU's variables (no device needed)")
    p.add_argument("mcu_name", metavar="mcu")
    p.add_argument("pattern", nargs="?", default="*", help="glob such as motor* (default: all)")

    p = sub.add_parser("ptype", help="show where a variable is and its type's layout (no device needed)")
    p.add_argument("expr", metavar="EXPR", help="a variable such as main:motor, or a type such as main:motor_t")

    p = sub.add_parser("read", help="read values; structs and arrays open to every field and cell")
    p.add_argument("exprs", nargs="*", metavar="EXPR", help="default: the workspace's watch list")
    p.add_argument("--tab", metavar="NAME", help="only the items in this watch tab")
    p.add_argument("--max-cells", type=int, default=100, metavar="N", help="cells shown per array (default 100)")

    p = sub.add_parser("write", help="write one value and confirm it by reading it back")
    p.add_argument("expr", metavar="EXPR")
    p.add_argument("value", help="42, -7, 0x1F, 1.5, true, an enum name ... (a hex integer for a float is its "
                                 "bit pattern). For a scaled watch item, in its shown units")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask, for items the workspace confirms first")

    p = sub.add_parser("dump", help="hex and ASCII dump of a variable or a memory range")
    p.add_argument("what", nargs="+", metavar="EXPR [LEN] | MCU ADDR LEN")
    p = sub.add_parser("poke", help="write raw bytes, lowest address first, and confirm them")
    p.add_argument("mcu_name", metavar="mcu")
    p.add_argument("addr", type=_int)
    p.add_argument("data", help="hex bytes, such as 01020304")

    p = sub.add_parser("send", help="send one SCPI command and print its reply")
    p.add_argument("command", nargs="+")
    reply = p.add_mutually_exclusive_group()
    reply.add_argument("--reply", dest="expect", action="store_true", default=None,
                       help="wait for a reply even if the header has no '?'")
    reply.add_argument("--no-reply", dest="expect", action="store_false", help="don't wait for a reply")
    p.add_argument("--expect", dest="pattern", metavar="REGEX",
                   help="check the reply: exit code 11 if no part of it matches REGEX or no reply came")

    p = sub.add_parser("watch", help="poll values and print one line per sample (Ctrl+C stops)")
    p.add_argument("exprs", nargs="*", metavar="EXPR", help="default: the workspace's watch list")
    p.add_argument("--tab", metavar="NAME", help="only the items in this watch tab")
    p.add_argument("-i", "--interval", type=float, metavar="MS",
                   help="poll interval (default: the workspace's poll_ms, else 500)")
    p.add_argument("-n", "--count", type=int, default=0, help="stop after this many samples (default: run until Ctrl+C)")
    p.add_argument("--csv", type=Path, metavar="FILE", help="also log every value read to a CSV file")

    p = sub.add_parser("console", help="interactive SCPI console")
    p.add_argument("--timestamps", action="store_true",
                   help="start every line with the time, to the millisecond, and echo each command sent")
    p.add_argument("--log", type=Path, metavar="FILE",
                   help="append every line sent and received, with date and time, to a text file")

    p = sub.add_parser("history", help="list the saved console history, newest last (no device needed)")
    p.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="show the last N (default 20, 0 for all)")
    p.add_argument("--clear", action="store_true", help="delete the whole history")

    p = sub.add_parser("init", help="save the target as a new workspace file (no device needed)")
    p.add_argument("file", type=Path)
    p.add_argument("--force", action="store_true", help="overwrite the file if it exists")

    sub.add_parser("list", help="show the workspace's watch tabs and their items (no device needed)")

    p = sub.add_parser("add", help="add an item to the workspace's watch list, or change one (no device needed)")
    p.add_argument("expr", metavar="EXPR")
    p.add_argument("--tab", metavar="NAME", help="the watch tab to add it to, made if it is new (default: the first)")
    p.add_argument("--format", choices=FORMATS, help="integers as dec, hex, bin or char")
    p.add_argument("--q", metavar="QFMT", help="fixed point, such as Q15, Q31 or Q3.12")
    p.add_argument("--scale", type=float, metavar="K", help="shown = raw * K + OFFSET")
    p.add_argument("--offset", type=float)
    p.add_argument("--unit")
    p.add_argument("--poll", metavar="MS|global|manual", help="polling for this item")
    p.add_argument("--read-only", action=argparse.BooleanOptionalAction, default=None,
                   help="refuse writes; --no-read-only allows them even when the workspace is read-only")
    p.add_argument("--min", type=float, help="lowest value a write may set, in shown units")
    p.add_argument("--max", type=float, help="highest value a write may set, in shown units")
    p.add_argument("--confirm", action=argparse.BooleanOptionalAction, default=None, help="ask before writing")

    p = sub.add_parser("remove", help="remove an item from the workspace's watch list (no device needed)")
    p.add_argument("expr", metavar="EXPR")
    p.add_argument("--tab", metavar="NAME", help="remove it from this watch tab only (default: from every tab)")

    p = sub.add_parser("read-only", help="show or set the workspace's read-only default (no device needed)")
    p.add_argument("state", nargs="?", choices=("on", "off"),
                   help="on: every item refuses writes unless it allows them (add --no-read-only)")

    p = sub.add_parser("tab", help="add, rename or remove a watch tab (no device needed)")
    tab = p.add_subparsers(dest="tab_cmd", metavar="ACTION", required=True)
    tab.add_parser("add", help="add an empty tab").add_argument("name")
    q = tab.add_parser("rename", help="rename a tab")
    q.add_argument("name")
    q.add_argument("new_name")
    tab.add_parser("remove", help="remove a tab and its items").add_argument("name")

    p = sub.add_parser("commands", help="the workspace's list of known SCPI commands (no device needed)")
    cmds = p.add_subparsers(dest="commands_cmd", metavar="ACTION", required=True)
    q = cmds.add_parser("list", help="show the list, by group")
    q.add_argument("text", nargs="?", default="", help="only commands, names or descriptions containing this")
    q = cmds.add_parser("import", help="add commands from a CSV or text file, skipping ones already listed")
    q.add_argument("file", type=Path)
    q.add_argument("--replace", action="store_true", help="replace the list instead of adding to it")
    cmds.add_parser("export", help="write the list to a CSV file").add_argument("file", type=Path)
    cmds.add_parser("clear", help="empty the list")
    return ap


LOCAL_TARGET = Path("rdt.toml")     # used when no -w/-t is given and the current folder has one


def load_workspace(args: argparse.Namespace) -> Workspace:
    if args.target is None and LOCAL_TARGET.is_file():
        args.target = LOCAL_TARGET
    ws = Workspace.load(args.target) if args.target else Workspace()
    cfg = ws.config
    if args.profile:
        cfg.profile = Profile.load(args.profile)
        cfg.profile_path = args.profile.resolve()
    if args.tcp:
        cfg.connection = ConnectionConfig("tcp", args.tcp)
    elif args.udp:
        cfg.connection = ConnectionConfig("udp", args.udp)
    elif args.serial:
        cfg.connection = ConnectionConfig("serial", args.serial, cfg.connection.baud)
    if args.baud:
        cfg.connection.baud = args.baud
    if args.timeout:
        cfg.profile.timeout_ms = args.timeout
        for m in cfg.mcus:
            m.timeout_ms = None
    if args.mcu:
        cfg.mcus = [McuConfig(m.id, m.name, m.elf.resolve() if m.elf else None) for m in args.mcu]
    if not cfg.mcus:
        raise ConfigError("no MCUs: give a workspace (-w), put an rdt.toml in the current folder, "
                          "or give --mcu NAME=ID[:ELF], such as --mcu main=0:main.elf")
    return ws


def load_config(args: argparse.Namespace) -> TargetConfig:
    return load_workspace(args).config


# --- commands --------------------------------------------------------------------------------

def cmd_info(t: Target, args, out) -> int:
    print(f"Link:          {t.config.connection.describe()}", file=out)
    print(f"Profile:       {t.profile.name}", file=out)
    print(f"Max response:  {t.max_response} bytes", file=out)
    for m in t.mcus.values():
        elf = m.elf.path.name if m.elf else "no ELF"
        print(f"MCU {m.id} {m.name} ({elf}, {m.byte_order} endian)", file=out)
        if m.identity is not None:
            print(f"    identity: {m.identity.status}, {m.identity.detail}", file=out)
        for a in m.memory.areas:
            kind = a.kind
            access = "rw" if a.writable else "ro"
            print(f"    0x{a.start:08X}  {a.size:8d}  {access}  {kind:10s}  {a.name}", file=out)
    return 0


def print_rows(t: Target, loc: Location, data: bytes, out, max_cells: int = 100) -> None:
    rows = t.rows(loc, data, max_cells)
    if len(rows) == 1 and rows[0].text is not None:
        print(f"{loc.path} = {rows[0].text}", file=out)
        return
    print(f"{loc.path}  {loc.type_name}, {loc.size} bytes at 0x{loc.addr:08X}", file=out)
    leaves = [r for r in rows if r.text is not None]
    width = max(len(r.path) for r in leaves) if leaves else 0
    vwidth = min(24, max((len(r.text) for r in leaves), default=0))
    for r in leaves:
        if r.value is None:
            print(f"{r.path:<{width}}   {r.text}", file=out)
        else:
            print(f"{r.path:<{width}} = {r.text:<{vwidth}}  {r.type_name}", file=out)


def print_shown(loc: Location, shown: list[Shown], out) -> None:
    if len(shown) == 1 and shown[0].path == loc.path:
        print(f"{loc.path} = {shown[0].text}", file=out)
        return
    print(f"{loc.path}  {loc.type_name}, {loc.size} bytes at 0x{loc.addr:08X}", file=out)
    width = max((len(s.path) for s in shown), default=0)
    for s in shown:
        print(f"{s.path:<{width}} = {s.text}", file=out)


def _exprs(args) -> list[str]:
    if args.exprs:
        if args.tab:
            raise ConfigError("name what to read, or give --tab, not both")
        return args.exprs
    if args.tab:
        tab = args.ws.tab(args.tab)
        if not tab.watches:
            raise ConfigError(f"tab {tab.name!r} has no items")
        return [w.expr for w in tab.watches]
    if not args.ws.watches:
        raise ConfigError("name what to read, or give a workspace (-w) with a watch list")
    return [w.expr for w in args.ws.watches]


def cmd_read(t: Target, args, out) -> int:
    failed = 0
    for text in _exprs(args):
        try:
            w = args.ws.watch(text)
            if w is not None:
                loc, shown = args.ws.read(t, w, args.max_cells)
            else:
                loc = t.resolve(text)
                data = t.read_location(loc)
        except RdtError as e:
            print(f"{text} : {e}", file=out)
            failed += 1
        else:
            if w is not None:
                print_shown(loc, shown, out)
            else:
                print_rows(t, loc, data, out, args.max_cells)
    return 1 if failed else 0


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_write(t: Target, args, out) -> int:
    loc, _ = args.ws.write(t, args.expr, args.value, confirm=(lambda q: True) if args.yes else _ask)
    _, shown = args.ws.read(t, args.ws.watch(loc.path) or loc.path)
    print(f"{loc.path} = {shown[0].text} (confirmed by read-back)", file=out)
    return 0


def hexdump(addr: int, data: bytes, width: int = 16) -> list[str]:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexes = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{addr + off:08X}  {hexes:<{3 * width - 1}}  |{text}|")
    return lines


def cmd_dump(t: Target, args, out) -> int:
    what = args.what
    if ":" in what[0]:
        if len(what) > 2:
            raise ConfigError("dump takes EXPR [LEN], or MCU ADDR LEN")
        loc = t.resolve(what[0])
        mcu, addr = loc.mcu, loc.addr
        length = _int(what[1]) if len(what) == 2 else loc.size
    else:
        if len(what) != 3:
            raise ConfigError("dump takes MCU ADDR LEN, or a name such as main:rx_buffer")
        try:
            mcu, addr, length = what[0], parse_int(what[1]), parse_int(what[2])
        except ValueError as e:
            raise ConfigError(str(e)) from None
    data = t.read(mcu, addr, length)
    for line in hexdump(addr, data):
        print(line, file=out)
    return 0


def cmd_symbols(t: Target, args, out) -> int:
    m = t.mcu(args.mcu_name)
    if m.symbols is None:
        raise ConfigError(f"MCU {m.name} has no ELF")
    found = m.symbols.search(args.pattern)
    width = max((len(v.type.name) for v in found), default=0)
    for v in found:
        print(f"0x{v.address:08X}  {v.size:6d}  {v.type.name:<{width}}  {v.qualified_name}", file=out)
    if not found:
        print(f"no variables match {args.pattern!r}", file=out)
        return 1
    return 0


def cmd_ptype(t: Target, args, out) -> int:
    try:
        loc = t.describe(args.expr)
    except SymbolError as e:
        # Not a variable: maybe a type, such as main:motor_t.
        mcu, _, name = args.expr.partition(":")
        typ = t.mcu(mcu).symbols.find_type(name) if ":" in args.expr and t.mcu(mcu).symbols else None
        if typ is None:
            raise e from None
        print(f"{typ.name}  {_kind(typ)}, {typ.size} bytes", file=out)
        _layout(typ, 0, 1, out)
        return 0
    var = None
    sym = t.mcu(loc.mcu).symbols
    if sym is not None and sym.at(loc.addr) is not None:
        var = sym.at(loc.addr)
    where = f" in {var.file_name}" if var and var.address == loc.addr and not loc.dynamic else ""
    bits = f", bits {loc.bit_shift}..{loc.bit_shift + loc.bit_size - 1} of {loc.storage} bytes" if loc.bit_size else ""
    print(f"{loc.path}  {loc.type_name}  {_kind(loc.type)}, {loc.type.size} bytes at 0x{loc.addr:08X}{bits}{where}",
          file=out)
    _layout(loc.type, 0, 1, out)
    return 0


def _kind(t: Type) -> str:
    s = t.strip()
    return s.name if s is not t else type(s).__name__.removesuffix("Type").lower()


def _layout(t: Type, base: int, depth: int, out) -> None:
    s = t.strip()
    pad = "  " * depth
    if isinstance(s, EnumType):
        print(f"{pad}{{{', '.join(f'{n} = {v}' for v, n in s.values.items())}}}", file=out)
    elif isinstance(s, ArrayType) and not is_char_array(s) and isinstance(s.element.strip(), (StructType, ArrayType)):
        print(f"{pad}each cell, {s.element.size} bytes:", file=out)
        _layout(s.element, 0, depth + 1, out)
    elif isinstance(s, StructType):
        members = s.named_members()
        width = max((len(m.name) for _, m in members), default=0)
        for off, m in members:
            o = base + off
            if m.is_bitfield:
                print(f"{pad}+0x{o:02X}  {m.name:<{width}}  {m.type.name} : {m.bit_size}  "
                      f"(bits {m.bit_shift}..{m.bit_shift + m.bit_size - 1} of {m.storage} bytes)", file=out)
                continue
            print(f"{pad}+0x{o:02X}  {m.name:<{width}}  {m.type.name}", file=out)
            inner = m.type.strip()
            if isinstance(inner, (StructType, EnumType)) or (isinstance(inner, ArrayType) and not is_char_array(inner)
                                                             and isinstance(inner.element.strip(), StructType)):
                _layout(m.type, 0, depth + 1, out)


def cmd_poke(t: Target, args, out) -> int:
    try:
        data = bytes.fromhex(args.data)
    except ValueError:
        raise ConfigError(f"{args.data!r} is not hex bytes") from None
    args.ws.check_raw_write(args.mcu_name, args.addr)
    t.write(args.mcu_name, args.addr, data)
    print(f"wrote {len(data)} bytes at 0x{args.addr:08X} on {args.mcu_name} (confirmed by read-back)", file=out)
    return 0


def _send(t: Target, text: str, expect: bool | None, out) -> int:
    reply = t.command(text, expect)
    is_query = expect if expect is not None else text.split(None, 1)[0].endswith("?")
    if reply is not None:
        print(reply, file=out)
        return 0
    if not is_query:
        return 0
    # The firmware sends no reply when a command fails.
    print("(no reply)", file=out)
    return 1


EXIT_EXPECT_FAILED = 11


def cmd_send(t: Target, args, out) -> int:
    text = " ".join(args.command)
    if args.pattern is None:
        return _send(t, text, args.expect, out)
    if args.expect is False:
        raise ConfigError("--expect needs a reply, so it can't go with --no-reply")
    try:
        pattern = re.compile(args.pattern)
    except re.error as e:
        raise ConfigError(f"--expect {args.pattern!r}: {e}") from None
    reply = t.command(text, True)
    if reply is None:
        print("(no reply)", file=out)
        print(f"rdt: expected a reply matching {args.pattern!r}, but none came", file=sys.stderr)
        return EXIT_EXPECT_FAILED
    print(reply, file=out)
    if not pattern.search(reply):
        print(f"rdt: the reply does not match {args.pattern!r}", file=sys.stderr)
        return EXIT_EXPECT_FAILED
    return 0


def _cells(r: Reading) -> dict[str, Shown]:
    """The leaf values of one watched item from one poll, by path."""
    return {s.path: s for s in r.shown or [] if s.raw is not None}


def _failure(e: Exception) -> str:
    return "link-down" if isinstance(e, LinkDown) else type(e).__name__


def cmd_watch(t: Target, args, out) -> int:
    ws: Workspace = args.ws
    items = [ws.watch(e) or Watch(e) for e in _exprs(args)]
    interval = args.interval if args.interval is not None else (ws.poll_ms or None)
    if interval is None:
        raise ConfigError("the workspace's polling is off (poll_ms = 0); give -i MS")
    every = 1 << 30
    first = ws.read_many(t, items, every, compact=True)
    columns: list[tuple[int, str]] = []
    last: list[dict[str, Shown] | str] = []
    locs: list[Location | None] = []
    units: dict[str, str] = {}
    for i, r in enumerate(first):
        if r.error is not None:
            raise r.error
        cells = _cells(r)
        if not cells:
            raise ConfigError(f"{r.watch.expr} has nothing to show")
        columns += [(i, p) for p in cells]
        units.update({p: r.watch.display.unit for p in cells if r.watch.display.unit})
        last.append(cells)
        locs.append(r.loc)
    log = CsvLog(args.csv, [p for _, p in columns], units) if args.csv else None
    # Each item's own interval (-i replaces the global one); the line is printed at the fastest.
    periods = [w.interval_ms(max(1, int(interval))) for w in items]
    tick = max(min((p for p in periods if p), default=interval), 1) / 1000.0
    due = [time.monotonic()] * len(items)
    print("time          " + "  ".join(p for _, p in columns), file=out)
    n = 0
    next_at = time.monotonic()
    next_elf_check = next_at + 1.0
    link_up = True
    try:
        while not args.count or n < args.count:
            now = time.monotonic()
            event = []
            if now >= next_elf_check:
                next_elf_check = now + 1.0
                for name in t.refresh_elfs():
                    event.append(f"ELF rebuilt for {name}")
                    for ident in t.check_identity(name):
                        if ident.status == "mismatch":
                            event.append(f"ELF does not match the firmware on {name}")
                            print(f"# {ident}", file=out, flush=True)
                    for i, loc in enumerate(locs):
                        if loc is not None and loc.mcu == name:
                            locs[i], note = t.remap(loc)
                            if note:
                                print(f"# {note}", file=out, flush=True)
            # Due times follow the planned ticks, not the clock, so a coarse clock (Windows' is
            # about 16 ms) or an early wake-up can't skip an item that is due at every tick.
            planned = next_at
            todo = [i for i in range(len(items)) if locs[i] is not None
                    and (n == 0 or (periods[i] is not None and planned + tick / 2 >= due[i]))]
            for i in range(len(items)):
                if locs[i] is None:
                    last[i] = "gone"
            readings = first if n == 0 else ws.read_many(t, [items[i] for i in todo], every, compact=True)
            values: dict[str, object] = {}
            when = time.time()
            for i, r in zip(todo, readings):
                due[i] = planned + (periods[i] or 0) / 1000.0
                when = min(when, r.time) if r.time else when
                if r.error is not None:
                    last[i] = _failure(r.error)
                    continue
                last[i] = _cells(r)
                values.update({p: s.value for p, s in last[i].items()})
            down = any(isinstance(r.error, LinkDown) for r in readings)
            if down == link_up:
                link_up = not down
                event.append("link down" if down else "link up")
            if log is not None and (values or event):
                log.write(when, values, "; ".join(event))
            stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S.%f")[:-3]
            cells = [g if isinstance(g, str) else (g[p].text if p in g else "gone")
                     for g, p in ((last[i], p) for i, p in columns)]
            print(f"{stamp}  " + "  ".join(f"{c:>{len(p)}}" for c, (_, p) in zip(cells, columns)), file=out, flush=True)
            n += 1
            next_at += tick
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # fall behind instead of queueing
    except KeyboardInterrupt:
        pass
    finally:
        if log is not None:
            log.close()
            print(f"# {log.rows} rows logged to {log.path}", file=out)
    return 0


def _need_file(ws: Workspace) -> Workspace:
    if ws.path is None:
        raise ConfigError("give the workspace file with -w FILE (rdt init FILE makes one)")
    return ws


def cmd_init(t: Target, args, out) -> int:
    if args.file.exists() and not args.force:
        raise ConfigError(f"{args.file} exists; add --force to overwrite it")
    path = args.ws.save(args.file)
    print(f"saved workspace {path} ({len(args.ws.config.mcus)} MCUs, {len(args.ws.watches)} watched items)", file=out)
    return 0


def cmd_list(t: Target, args, out) -> int:
    ws = _need_file(args.ws)
    poll = f"every {ws.poll_ms} ms" if ws.poll_ms else "off"
    tabs = f"{len(ws.tabs)} tab{'' if len(ws.tabs) == 1 else 's'}"
    ro = ", every item read-only unless it allows writes" if ws.read_only else ""
    print(f"{ws.path}: {len(ws.watches)} watched items in {tabs}, polling {poll}{ro}", file=out)
    width = max((len(w.expr) for w in ws.watches), default=0)
    for tab in ws.tabs:
        print(f"tab {tab.name!r}: {len(tab.watches)} items", file=out)
        for w in tab.watches:
            notes = [f"{k}={v}" for k, v in w.to_dict().items() if k != "expr"]
            print(f"  {w.expr:<{width}}  {' '.join(notes)}".rstrip(), file=out)
    return 0


def cmd_add(t: Target, args, out) -> int:
    ws = _need_file(args.ws)
    try:
        t.describe(args.expr)
    except SymbolError as e:
        if "needs the device" not in str(e):
            raise
    old = ws.watch(args.expr)
    d = old.to_dict() if old else {"expr": args.expr}
    for key, value in (("format", args.format), ("q", args.q), ("scale", args.scale), ("offset", args.offset),
                       ("unit", args.unit), ("read_only", args.read_only), ("min", args.min), ("max", args.max),
                       ("confirm", args.confirm)):
        if value is not None:
            d[key] = value
    if args.poll is not None:
        d["poll"] = args.poll if args.poll in ("global", "manual") else _int(args.poll)
    w = Watch.from_dict(d)
    if old:
        ws.replace(w)
    if args.tab is not None and not ws.has_tab(args.tab):
        ws.add_tab(args.tab)
    if args.tab is not None:
        tab = ws.tab(args.tab)
    else:   # without --tab, an item already watched is changed where it is
        tab = ws.tabs_with(w.expr)[0] if old else ws.ensure_tab()
    added = tab.watch(w.expr) is None
    if added:
        ws.add(w, tab.name)
    ws.save()
    what = "added" if added else "changed"
    if added and old:
        what = "added (with its settings from another tab)" if d == old.to_dict() else "changed and added"
    print(f"{what} {w.expr} in tab {tab.name!r} of {ws.path}", file=out)
    return 0


def cmd_remove(t: Target, args, out) -> int:
    ws = _need_file(args.ws)
    w = ws.remove(args.expr, args.tab)
    ws.save()
    where = f"tab {ws.tab(args.tab).name!r} of " if args.tab else ""
    print(f"removed {w.expr} from {where}{ws.path}", file=out)
    return 0


def cmd_read_only(t: Target, args, out) -> int:
    ws = _need_file(args.ws)
    if args.state is not None:
        ws.read_only = args.state == "on"
        ws.save()
    if ws.read_only:
        print(f"{ws.path}: every item is read-only unless it allows writes; raw writes are refused", file=out)
    else:
        print(f"{ws.path}: items are writable unless they are read-only", file=out)
    return 0


def cmd_tab(t: Target, args, out) -> int:
    ws = _need_file(args.ws)
    if args.tab_cmd == "add":
        tab = ws.add_tab(args.name)
        msg = f"added tab {tab.name!r}"
    elif args.tab_cmd == "rename":
        old = ws.tab(args.name).name
        tab = ws.rename_tab(args.name, args.new_name)
        msg = f"renamed tab {old!r} to {tab.name!r}"
    else:
        tab = ws.remove_tab(args.name)
        msg = f"removed tab {tab.name!r} and its {len(tab.watches)} items"
    ws.save()
    print(f"{msg} in {ws.path}", file=out)
    return 0


def _print_commands(commands: list[SavedCommand], text: str, out) -> None:
    needle = text.casefold()
    shown = [c for c in commands if needle in f"{c.command} {c.name} {c.description}".casefold()]
    group = None
    for c in sorted(shown, key=lambda c: c.group.casefold()):
        if c.group != group:
            group = c.group
            print(f"[{group or 'no group'}]", file=out)
        extra = " - ".join(x for x in (c.name, c.description) if x)
        print(f"  {c.command}" + (f"    {extra}" if extra else ""), file=out)
    if not shown:
        print("(no saved commands" + (f" matching {text!r}" if text else "") + ")", file=out)


def cmd_commands(t: Target | None, args, out) -> int:
    ws = args.ws
    if args.commands_cmd == "list":
        _print_commands(ws.commands, args.text, out)
        return 0
    if args.commands_cmd == "export":
        try:
            write_commands(args.file, ws.commands)
        except OSError as e:
            raise ConfigError(f"cannot write {args.file}: {e.strerror}") from None
        print(f"wrote {len(ws.commands)} commands to {args.file}", file=out)
        return 0
    ws = _need_file(ws)
    if args.commands_cmd == "clear":
        n = len(ws.commands)
        ws.commands = []
        msg = f"removed {n} commands"
    else:
        try:
            new = read_commands(args.file)
        except OSError as e:
            raise ConfigError(f"cannot read {args.file}: {e.strerror}") from None
        except (ValueError, csv.Error) as e:
            raise ConfigError(str(e)) from None
        ws.commands, added, skipped = merge_commands(ws.commands, new, args.replace)
        msg = f"imported {added} commands" + (f", skipped {skipped} already listed" if skipped else "")
    ws.save()
    print(f"{msg} in {ws.path}", file=out)
    return 0


CONSOLE_HELP = """Type any SCPI command; queries (header ending in '?') wait for a reply.
  .read EXPR...         read values, such as .read main:motor @main:0x20000010:u32
  .write EXPR VALUE     write a value and confirm it
  .history [N|clear]    the saved command history (last 20, or N), or delete it
  .commands [TEXT]      the workspace's known commands (those containing TEXT)
  .info                 the device, max response size and each MCU's memory, as rdt info
  .reconnect            close the link and open it again, such as after a board reset
  .break                send a 250 ms break (COM port only)
  !N                    send history entry N again
  .help                 this text
  .quit                 leave (or Ctrl+D)
Up and Down walk the history; Tab completes from the history, the known commands, the profile and
common SCPI commands."""
CONSOLE_DOT_COMMANDS = (".read ", ".write ", ".history", ".commands", ".info", ".reconnect", ".break",
                        ".help", ".quit")


class _Logged:
    """An output stream that also copies every finished line to the session log, as a reply, or as
    an error for the console's error lines."""

    def __init__(self, out, log: SessionLog):
        self.out = out
        self.log = log
        self.pending = ""

    def write(self, text: str) -> int:
        self.out.write(text)
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        for line in lines:
            if line.startswith("> "):
                kind = "tx"
            elif line.startswith(("error:", "(no reply")):
                kind = "error"
            else:
                kind = "rx"
            self.log.write(kind, line[2:] if kind == "tx" else line)
        return len(text)

    def flush(self) -> None:
        self.out.flush()


class _Stamped:
    """An output stream that starts every line with the time, for ``rdt console --timestamps``."""

    def __init__(self, out):
        self.out = out
        self.at_line_start = True

    def write(self, text: str) -> int:
        for part in text.splitlines(keepends=True):
            if self.at_line_start:
                self.out.write(datetime.now().strftime("%H:%M:%S.%f")[:-3] + "  ")
            self.out.write(part)
            self.at_line_start = part.endswith("\n")
        return len(text)

    def flush(self) -> None:
        self.out.flush()


def _setup_line_editing(history: CommandHistory, profile: Profile, saved: list[str]) -> None:
    """On a terminal: Up and Down over the saved history, Tab completion. Needs readline
    (pyreadline3 on Windows); without it the console still works, without either."""
    try:
        import readline
    except ImportError:
        return
    readline.clear_history()
    for c in history.items:
        readline.add_history(c)
    matches: list[str] = []

    def complete(text: str, state: int) -> str | None:
        if state == 0:
            line = readline.get_line_buffer()
            matches[:] = completions(line, history.items, None, [*saved, *CONSOLE_DOT_COMMANDS], common=False)
        return matches[state] if state < len(matches) else None

    readline.set_completer_delims("")      # complete the whole line: SCPI headers hold : ? *
    readline.set_completer(complete)
    readline.parse_and_bind("tab: complete")


def _print_history(history: CommandHistory, limit: int, out) -> None:
    items = history.items
    first = max(0, len(items) - limit) if limit > 0 else 0
    for i in range(first, len(items)):
        print(f"{i + 1:4}  {items[i]}", file=out)
    if not items:
        print("(the console history is empty)", file=out)


def cmd_history(t: Target | None, args, out) -> int:
    history = CommandHistory()
    if args.clear:
        history.clear()
        print(f"cleared the console history in {history.path}", file=out)
    else:
        _print_history(history, args.limit, out)
    return 0


def cmd_console(t: Target, args, out) -> int:
    history = CommandHistory()
    interactive = sys.stdin.isatty()
    if interactive:
        _setup_line_editing(history, t.profile, [c.command for c in args.ws.commands])
    session_log = None
    if getattr(args, "log", None):
        try:
            session_log = SessionLog(args.log)
        except OSError as e:
            raise ConfigError(f"cannot write the console log {args.log}: {e}") from None
    if getattr(args, "timestamps", False):
        out = _Stamped(out)
    banner = f"Connected to {t.config.connection.describe()}. .help for help."
    print(banner, file=out)
    if session_log is not None:
        session_log.write("note", banner)
        out = _Logged(out, session_log)
    try:
        return _console_loop(t, args, history, interactive, out)
    finally:
        if session_log is not None:
            session_log.close()


def _console_loop(t: Target, args, history: CommandHistory, interactive: bool, out) -> int:
    prompt = "rdt> " if interactive else ""
    while True:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            break
        line = line.strip()
        if not line:
            continue
        try:
            if line.startswith("!"):
                n = line[1:].strip()
                if not n.isdigit() or not 1 <= int(n) <= len(history.items):
                    raise ConfigError(f"no history entry {n!r}; .history lists them")
                line = history.items[int(n) - 1]
                print(f"> {line}", file=out)
            elif getattr(args, "timestamps", False):
                print(f"> {line}", file=out)
            elif isinstance(out, _Logged):
                out.log.write("tx", line)
            if line in (".quit", ".exit"):
                break
            if line == ".help":
                print(CONSOLE_HELP, file=out)
            elif line == ".history clear":
                history.clear()
                print("console history cleared", file=out)
            elif line == ".history" or line.startswith(".history "):
                arg = line[len(".history"):].strip() or "20"
                if not arg.isdigit():
                    raise ConfigError(".history takes a count or 'clear'")
                _print_history(history, int(arg), out)
            elif line == ".info":
                cmd_info(t, args, out)
            elif line == ".reconnect":
                t.reconnect()
                print(f"reconnected to {t.config.connection.describe()}", file=out)
                for m in t.mcus.values():
                    if m.identity is not None and m.identity.status == "mismatch":
                        if args.allow_mismatch:
                            t.allow_mismatch(m)
                        print(f"warning: {m.identity}", file=out)
            elif line == ".break":
                t.send_break()
                print("break sent (250 ms)", file=out)
            elif line == ".commands" or line.startswith(".commands "):
                _print_commands(args.ws.commands, line[len(".commands"):].strip(), out)
            elif line.startswith(".read "):
                for text in line.split()[1:]:
                    loc = t.resolve(text)
                    print_rows(t, loc, t.read_location(loc), out)
            elif line.startswith(".write "):
                parts = line.split(None, 2)
                if len(parts) != 3:
                    raise ConfigError(".write takes EXPR VALUE")
                args.expr, args.value, args.yes = parts[1], parts[2], False
                cmd_write(t, args, out)
            else:
                history.add(line)
                _send(t, line, None, out)
        except RdtError as e:
            print(f"error: {e}", file=out)
    return 0


COMMANDS = {
    "init": cmd_init, "list": cmd_list, "add": cmd_add, "remove": cmd_remove, "tab": cmd_tab,
    "read-only": cmd_read_only,
    "info": cmd_info, "symbols": cmd_symbols, "ptype": cmd_ptype, "read": cmd_read, "write": cmd_write, "dump": cmd_dump, "poke": cmd_poke,
    "send": cmd_send, "watch": cmd_watch, "console": cmd_console, "history": cmd_history, "commands": cmd_commands,
}


OFFLINE = ("symbols", "ptype", "init", "list", "add", "remove", "tab", "commands", "read-only")   # never open the link


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    level = [logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    try:
        if args.cmd == "history":          # needs no workspace or device
            return cmd_history(None, args, out)
        args.ws = load_workspace(args)
        for warning in args.ws.warnings:
            print(f"rdt: {warning}", file=sys.stderr)
        cfg = args.ws.config
        # One-shot commands fail at once on a broken link; watch and console keep reconnecting.
        target = Target(cfg, auto_reconnect=args.cmd in ("watch", "console"))
        if args.cmd in OFFLINE:
            return COMMANDS[args.cmd](target, args, out)
        target.connect()
        for m in target.mcus.values():
            if m.identity is not None and m.identity.status == "mismatch":
                if args.allow_mismatch:
                    target.allow_mismatch(m)
                print(f"rdt: warning: {m.identity}" +
                      ("" if args.allow_mismatch else "; reads and writes to it are refused (--allow-mismatch to access it anyway)"),
                      file=sys.stderr)
        try:
            return COMMANDS[args.cmd](target, args, out)
        finally:
            target.close()
    except RdtError as e:
        print(f"rdt: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
