"""Command line: python -m sim [--config FILE]. Without --config it uses sim.toml in the current
folder when there is one, else sim/examples/two_mcus.toml."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import load_config
from .transports import Simulator

EXAMPLE_CONFIG = Path(__file__).parent / "examples" / "two_mcus.toml"
LOCAL_CONFIG = Path("sim.toml")     # your own config, picked up from the current folder


def default_config() -> Path:
    return LOCAL_CONFIG if LOCAL_CONFIG.is_file() else EXAMPLE_CONFIG


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim", description="Simulated RDT device (SCPI memory commands).")
    ap.add_argument("--config", type=Path,
                    help="simulator TOML (default: sim.toml in the current folder, else the two-MCU example)")
    ap.add_argument("--tcp", help="HOST:PORT to listen on for TCP, or 'off'")
    ap.add_argument("--udp", help="HOST:PORT to listen on for UDP, or 'off'")
    ap.add_argument("--serial", help="serial port to serve, such as COM11, 'pty' on Linux, or 'off'")
    ap.add_argument("--baud", type=int, help="serial baud rate")
    ap.add_argument("--udp-drop", type=float, help="fraction of UDP requests to drop (0..1)")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="-v: connections, -vv: every command")
    args = ap.parse_args(argv)

    level = [logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(asctime)s %(message)s")

    config = args.config or default_config()
    cfg = load_config(config)
    t = cfg.transport
    for name in ("tcp", "udp", "serial"):
        value = getattr(args, name)
        if value is not None:
            setattr(t, name, None if value.lower() == "off" else value)
    if args.baud is not None:
        t.baud = args.baud
    if args.udp_drop is not None:
        t.udp_drop = args.udp_drop

    sim = Simulator(cfg.device, t).start()
    print(f"RDT simulator: {cfg.device.idn}")
    print(f"  Config {config.resolve()}")
    for mcu in sorted(cfg.device.mcus.values(), key=lambda m: m.id):
        regions = ", ".join(f"{r.name} 0x{r.start:08X}+{len(r.data)}" for r in mcu.memory.regions)
        role = "main" if mcu.is_main else f"secondary, {mcu.frame_size}-byte frames"
        print(f"  MCU {mcu.id} {mcu.name!r} ({role}, {mcu.byte_order} endian): {regions}")
    if sim.tcp_address:
        print("  TCP    %s:%d" % sim.tcp_address)
    if sim.udp_address:
        print("  UDP    %s:%d" % sim.udp_address)
    if sim.serial_port:
        print(f"  Serial {sim.serial_port} at {t.baud} baud")
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()
    try:
        while True:  # a plain sleep loop, so Ctrl+C also works on Windows
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
