"""Simulated MCUs: memory, live signals, and the 64-byte internal channel to secondary MCUs."""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from typing import Callable

from .memory import Memory, MemoryFault

SIGNAL_TYPES = {
    "u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i",
    "u64": "Q", "i64": "q", "f32": "f", "f64": "d",
}
SIGNAL_KINDS = ("counter", "sine", "square", "sawtooth")


@dataclass
class Signal:
    """A variable the simulated firmware keeps changing, so polling and charts have something to show.

    Its value is a function of time, computed whenever the memory under it is read.
    """

    addr: int
    type: str = "u32"
    kind: str = "counter"
    period_ms: float = 1000.0
    amplitude: float = 1.0
    offset: float = 0.0
    step: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        if self.type not in SIGNAL_TYPES:
            raise ValueError(f"signal type must be one of {', '.join(SIGNAL_TYPES)}, not {self.type!r}")
        if self.kind not in SIGNAL_KINDS:
            raise ValueError(f"signal kind must be one of {', '.join(SIGNAL_KINDS)}, not {self.kind!r}")
        if self.period_ms <= 0:
            raise ValueError("signal period_ms must be positive")

    @property
    def size(self) -> int:
        return struct.calcsize(SIGNAL_TYPES[self.type])

    def value(self, t: float) -> float:
        """Value at ``t`` seconds after the simulator started. A counter adds ``step`` every period."""
        period = self.period_ms / 1000.0
        phase = (t % period) / period
        if self.kind == "counter":
            return self.offset + self.step * math.floor(t / period)
        if self.kind == "sine":
            return self.offset + self.amplitude * math.sin(2 * math.pi * phase)
        if self.kind == "square":
            return self.offset + (self.amplitude if phase < 0.5 else -self.amplitude)
        return self.offset + self.amplitude * (2 * phase - 1)

    def encode(self, value: float, byte_order: str) -> bytes:
        code = SIGNAL_TYPES[self.type]
        prefix = "<" if byte_order == "little" else ">"
        if code in "fd":
            return struct.pack(prefix + code, value)
        bits = 8 * self.size
        raw = int(round(value)) & ((1 << bits) - 1)  # wrap like the C integer would
        return raw.to_bytes(self.size, byte_order)


class Mcu:
    """One MCU of the device.

    MCU 0 is the main MCU and owns the external link. Every other MCU sits behind the internal
    channel, which carries fixed frames of ``frame_size`` bytes, so the main MCU splits a larger
    read into pieces. Live signals are refreshed before each piece, so a large struct read from a
    secondary MCU can mix values from different moments, as on the real device.
    """

    def __init__(
        self,
        id: int,
        name: str,
        memory: Memory,
        byte_order: str = "little",
        signals: list[Signal] | None = None,
        frame_size: int = 64,
        frame_latency_ms: float = 0.0,
        silent: bool = False,
        clock: Callable[[], float] | None = None,
    ):
        if byte_order not in ("little", "big"):
            raise ValueError(f"byte_order must be little or big, not {byte_order!r}")
        if frame_size < 1:
            raise ValueError("frame_size must be at least 1")
        self.id = id
        self.name = name
        self.memory = memory
        self.byte_order = byte_order
        self.signals = list(signals or [])
        for sig in self.signals:
            # Every read refreshes every signal, so one outside memory would break all reads of
            # this MCU; refuse it here, as a config error (rani, 2026-10-18, code review).
            try:
                memory.read(sig.addr, sig.size)
            except MemoryFault:
                label = f"signal {sig.name}" if sig.name else "a signal"
                raise ValueError(f"MCU {id}: {label} at 0x{sig.addr:X} ({sig.size} bytes) is not in its memory") from None
        self.frame_size = frame_size
        self.frame_latency_ms = frame_latency_ms
        self.silent = silent
        start = time.monotonic()
        self.clock = clock or (lambda: time.monotonic() - start)
        self.frames = 0  # internal-channel frames exchanged, for tests and the log

    @property
    def is_main(self) -> bool:
        return self.id == 0

    def refresh_signals(self) -> None:
        t = self.clock()
        for s in self.signals:
            self.memory.write(s.addr, s.encode(s.value(t), self.byte_order), force=True)

    def _frame(self) -> None:
        self.frames += 1
        if self.frame_latency_ms:
            time.sleep(self.frame_latency_ms / 1000.0)

    def read(self, addr: int, size: int) -> bytes:
        if self.is_main:
            self.refresh_signals()
            return self.memory.read(addr, size)
        out = bytearray()
        for piece in range(addr, addr + size, self.frame_size):
            n = min(self.frame_size, addr + size - piece)
            self._frame()
            self.refresh_signals()
            out += self.memory.read(piece, n)
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        if not self.is_main:
            self._frame()
        self.memory.write(addr, data)

    def reset(self) -> None:
        self.memory.reset()
