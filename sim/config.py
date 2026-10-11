"""Loads the simulator's TOML config: the device, its MCUs and the transports to listen on."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .device import Device
from .mcu import Mcu, Signal
from .memory import Memory, Region
from .profile import Profile


@dataclass
class TransportConfig:
    tcp: str | None = "127.0.0.1:5025"
    udp: str | None = "127.0.0.1:5025"
    serial: str | None = None  # a port name such as COM11, or "pty" for a pseudo-terminal on Linux
    baud: int = 115200
    udp_drop: float = 0.0  # fraction of UDP requests silently dropped, to exercise host retries


@dataclass
class SimConfig:
    device: Device
    transport: TransportConfig = field(default_factory=TransportConfig)


def parse_endpoint(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep:
        return "127.0.0.1", int(text)
    return host or "127.0.0.1", int(port)


def _build_mcu(table: dict, base: Path) -> Mcu:
    if "id" not in table:
        raise ValueError("every [[mcu]] needs an id")
    mcu_id = int(table["id"])
    memory = Memory()
    byte_order = None
    if table.get("elf"):
        byte_order = memory.load_elf(base / table["elf"])
    for r in table.get("ranges", []):
        fill = int(r.get("fill", 0)) & 0xFF
        memory.add(Region(r.get("name", "range"), int(r["start"]), bytearray([fill]) * int(r["size"]),
                          bool(r.get("writable", True))))
    if not memory.regions:
        raise ValueError(f"MCU {mcu_id} has no memory: give it an elf or [[mcu.ranges]]")
    signals = [Signal(**s) for s in table.get("signals", [])]
    return Mcu(
        id=mcu_id,
        name=table.get("name", f"mcu{mcu_id}"),
        memory=memory,
        byte_order=table.get("byte_order", byte_order or "little"),
        signals=signals,
        frame_size=int(table.get("frame_size", 64)),
        frame_latency_ms=float(table.get("frame_latency_ms", 0.0)),
        silent=bool(table.get("silent", False)),
    )


def load_config(path: str | Path) -> SimConfig:
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    base = path.parent
    if raw.get("profile"):
        profile = Profile.load(base / raw["profile"])
    else:
        profile = Profile.from_dict(raw.get("scpi", {}))
    mcus = [_build_mcu(t, base) for t in raw.get("mcu", [])]
    device = Device(
        mcus,
        profile,
        idn=raw.get("idn", "RDT,Simulator,0,0.1"),
        internal_timeout_ms=float(raw.get("internal_timeout_ms", 50.0)),
        error_reply=bool(raw.get("error_reply", False)),
    )
    transport = TransportConfig(**raw.get("transport", {}))
    return SimConfig(device, transport)
