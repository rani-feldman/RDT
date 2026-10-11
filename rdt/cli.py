"""Command line: rdt [connection options] COMMAND ...

    rdt -t bench.toml info
    rdt -t bench.toml read @main:0x20000010:u32 @motorctl:0x20000088:f32
    rdt -t bench.toml write @main:0x20000004:f32 2500
    rdt -t bench.toml dump main 0x20000000 64
    rdt -t bench.toml send "*IDN?"
    rdt -t bench.toml watch @main:0x20000000:f32 -i 100
    rdt -t bench.toml console

Without a target file, give the link and the MCUs on the command line:

    rdt --tcp 192.168.1.20:5025 --profile fw.toml --mcu main=0:main.elf --mcu motorctl=1:motorctl.elf info
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, codec
from .errors import CommandTimeout, ConfigError, LinkDown, RdtError
from .profile import Profile, parse_int
from .target import ConnectionConfig, McuConfig, Target, TargetConfig, parse_ref


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
        epilog="Addresses are written @mcu:address:type, such as @main:0x20000010:u32. "
               f"Types: {', '.join(codec.TYPES)}.",
    )
    ap.add_argument("--version", action="version", version=f"rdt {__version__}")
    g = ap.add_argument_group("target")
    g.add_argument("-t", "--target", type=Path, help="target file (TOML): connection, profile and MCUs")
    link = g.add_mutually_exclusive_group()
    link.add_argument("--tcp", metavar="HOST:PORT", help="connect over LAN TCP")
    link.add_argument("--udp", metavar="HOST:PORT", help="connect over LAN UDP")
    link.add_argument("--serial", metavar="PORT", help="connect over a COM port, such as COM12")
    g.add_argument("--baud", type=int, help="COM port baud rate (default 115200)")
    g.add_argument("--profile", type=Path, help="SCPI profile (TOML with an [scpi] table)")
    g.add_argument("--mcu", type=_mcu_option, action="append", metavar="NAME=ID[:ELF]",
                   help="an MCU of the device; repeat for each. Replaces the target file's MCUs")
    g.add_argument("--timeout", type=float, metavar="MS", help="reply timeout in ms, for every MCU")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v: link events, -vv: every command and reply")

    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    sub.add_parser("info", help="identify the device and list its MCUs and memory")

    p = sub.add_parser("read", help="read values at raw addresses")
    p.add_argument("refs", nargs="+", metavar="@MCU:ADDR:TYPE")

    p = sub.add_parser("write", help="write a value and confirm it by reading it back")
    p.add_argument("ref", metavar="@MCU:ADDR:TYPE")
    p.add_argument("value", help="42, -7, 0x1F, 1.5 ... (a hex integer for a float is its bit pattern)")

    p = sub.add_parser("dump", help="hex and ASCII dump of a memory range")
    p.add_argument("mcu_name", metavar="mcu")
    p.add_argument("addr", type=_int)
    p.add_argument("length", type=_int)

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

    sub.add_parser("errors", help="read the SCPI error queue until it is empty")

    p = sub.add_parser("watch", help="poll values and print one line per sample (Ctrl+C stops)")
    p.add_argument("refs", nargs="+", metavar="@MCU:ADDR:TYPE")
    p.add_argument("-i", "--interval", type=float, default=500, metavar="MS", help="poll interval (default 500)")
    p.add_argument("-n", "--count", type=int, default=0, help="stop after this many samples (default: run until Ctrl+C)")

    sub.add_parser("console", help="interactive SCPI console")
    return ap


def load_config(args: argparse.Namespace) -> TargetConfig:
    cfg = TargetConfig.load(args.target) if args.target else TargetConfig()
    if args.profile:
        cfg.profile = Profile.load(args.profile)
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
        cfg.mcus = args.mcu
    if not cfg.mcus:
        raise ConfigError("no MCUs: give a target file (-t) or --mcu NAME=ID[:ELF], such as --mcu main=0:main.elf")
    return cfg


# --- commands --------------------------------------------------------------------------------

def cmd_info(t: Target, args, out) -> int:
    print(f"Link:          {t.config.connection.describe()}", file=out)
    print(f"Device:        {t.idn or '(no probe)'}", file=out)
    print(f"Profile:       {t.profile.name}", file=out)
    print(f"Max response:  {t.max_response} bytes", file=out)
    for m in t.mcus.values():
        elf = m.elf.path.name if m.elf else "no ELF"
        print(f"MCU {m.id} {m.name} ({elf}, {m.byte_order} endian)", file=out)
        for a in m.memory.areas:
            kind = "range" if a.peripheral else "section"
            access = "rw" if a.writable else "ro"
            print(f"    0x{a.start:08X}  {a.size:8d}  {access}  {kind:7s}  {a.name}", file=out)
    return 0


def cmd_read(t: Target, args, out) -> int:
    refs = [parse_ref(r) for r in args.refs]
    failed = 0
    for r in refs:
        try:
            v = t.read_value(r)
        except RdtError as e:
            print(f"{r} : {e}", file=out)
            failed += 1
        else:
            print(f"{r} = {codec.format_value(v, r.type)}", file=out)
    return 1 if failed else 0


def cmd_write(t: Target, args, out) -> int:
    r = parse_ref(args.ref)
    try:
        value = codec.parse_value(args.value, r.type)
        codec.encode(value, r.type, "little")
    except ValueError as e:
        raise ConfigError(str(e)) from None
    t.write_value(r, value)
    print(f"{r} = {codec.format_value(value, r.type)} (confirmed by read-back)", file=out)
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
    data = t.read(args.mcu_name, args.addr, args.length)
    for line in hexdump(args.addr, data):
        print(line, file=out)
    return 0


def cmd_poke(t: Target, args, out) -> int:
    try:
        data = bytes.fromhex(args.data)
    except ValueError:
        raise ConfigError(f"{args.data!r} is not hex bytes") from None
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
    # The firmware sends no reply when a command fails; the error queue may say why.
    try:
        err = t.error_query()
    except (CommandTimeout, LinkDown):
        err = None
    print("(no reply" + (f"; error queue: {err}" if err else "") + ")", file=out)
    return 1


def cmd_send(t: Target, args, out) -> int:
    return _send(t, " ".join(args.command), args.expect, out)


def cmd_errors(t: Target, args, out) -> int:
    for _ in range(64):
        err = t.error_query()
        if err is None:
            print('0,"No error"', file=out)
            return 0
        print(err, file=out)
    return 0


def cmd_watch(t: Target, args, out) -> int:
    refs = [parse_ref(r) for r in args.refs]
    for r in refs:
        t.mcu(r.mcu)
    print("time          " + "  ".join(str(r) for r in refs), file=out)
    period = max(args.interval, 1) / 1000.0
    n = 0
    next_at = time.monotonic()
    try:
        while not args.count or n < args.count:
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            cells = []
            for r in refs:
                try:
                    cells.append(codec.format_value(t.read_value(r), r.type, hex_too=False))
                except LinkDown:
                    cells.append("link-down")
                except RdtError as e:
                    cells.append(type(e).__name__)
            print(f"{stamp}  " + "  ".join(f"{c:>{len(str(r))}}" for c, r in zip(cells, refs)), file=out, flush=True)
            n += 1
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # fall behind instead of queueing
    except KeyboardInterrupt:
        pass
    return 0


CONSOLE_HELP = """Type any SCPI command; queries (header ending in '?') wait for a reply.
  .err    read the error queue        .read @mcu:addr:type   read a value
  .help   this text                   .quit                  leave (or Ctrl+D)"""


def cmd_console(t: Target, args, out) -> int:
    try:
        import readline  # noqa: F401  (history and line editing where available)
    except ImportError:
        pass
    print(f"Connected to {t.idn or t.config.connection.describe()}. .help for help.", file=out)
    prompt = "rdt> " if sys.stdin.isatty() else ""
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
            if line in (".quit", ".exit"):
                break
            if line == ".help":
                print(CONSOLE_HELP, file=out)
            elif line == ".err":
                err = t.error_query()
                print(err if err else '0,"No error"', file=out)
            elif line.startswith(".read "):
                for ref in line.split()[1:]:
                    r = parse_ref(ref)
                    print(f"{r} = {codec.format_value(t.read_value(r), r.type)}", file=out)
            else:
                _send(t, line, None, out)
        except RdtError as e:
            print(f"error: {e}", file=out)
    return 0


COMMANDS = {
    "info": cmd_info, "read": cmd_read, "write": cmd_write, "dump": cmd_dump, "poke": cmd_poke,
    "send": cmd_send, "errors": cmd_errors, "watch": cmd_watch, "console": cmd_console,
}


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    level = [logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    try:
        cfg = load_config(args)
        # One-shot commands fail at once on a broken link; watch and console keep reconnecting.
        target = Target(cfg, auto_reconnect=args.cmd in ("watch", "console"))
        target.connect()
        try:
            return COMMANDS[args.cmd](target, args, out)
        finally:
            target.close()
    except RdtError as e:
        print(f"rdt: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
