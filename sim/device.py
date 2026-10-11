"""The simulated device: the main MCU's SCPI command handler.

Commands from every transport and client go through one lock, as the firmware's single SCPI
parser would handle them one at a time. The SCPI error queue is shared by all clients.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .mcu import Mcu
from .memory import MemoryFault
from .profile import CommandPattern, Profile, int_base_for, parse_int

log = logging.getLogger("sim")

ERROR_QUEUE_SIZE = 16
WRITE_SIZES = (1, 2, 4, 8)


class ScpiError(Exception):
    def __init__(self, code: int, message: str, detail: str = ""):
        super().__init__(code, message, detail)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        text = f"{self.message};{self.detail}" if self.detail else self.message
        return f'{self.code},"{text}"'


# Standard SCPI error codes, plus the defined error for a secondary MCU that does not answer.
def _syntax(detail=""): return ScpiError(-102, "Syntax error", detail)
def _data_type(detail=""): return ScpiError(-104, "Data type error", detail)
def _undefined_header(detail=""): return ScpiError(-113, "Undefined header", detail)
def _out_of_range(detail=""): return ScpiError(-222, "Data out of range", detail)
def _too_much_data(detail=""): return ScpiError(-223, "Too much data", detail)
def _illegal_value(detail=""): return ScpiError(-224, "Illegal parameter value", detail)
def _mcu_not_responding(): return ScpiError(-240, "MCU not responding")


class Device:
    def __init__(
        self,
        mcus: list[Mcu],
        profile: Profile | None = None,
        idn: str = "RDT,Simulator,0,0.1",
        internal_timeout_ms: float = 50.0,
        error_reply: bool = False,
    ):
        ids = [m.id for m in mcus]
        if 0 not in ids:
            raise ValueError("MCU 0 (the main MCU) is required")
        if len(set(ids)) != len(ids):
            raise ValueError("MCU ids must be unique")
        self.mcus = {m.id: m for m in mcus}
        self.profile = profile or Profile()
        self.idn = idn
        self.internal_timeout_ms = internal_timeout_ms
        self.error_reply = error_reply
        self.errors: deque[ScpiError] = deque()
        self.lock = threading.Lock()

        p = self.profile
        self._addr_base = int_base_for(p.addr_format, "addr")
        self._value_base = int_base_for(p.value_format, "value")
        # (template, handler, replies). Profile templates come first, so a user command wins over
        # a built-in with the same header. The read always replies, even if its header has no "?".
        commands = [
            (p.read, self._read, True),
            (p.write, self._write, False),
            (p.size_query, lambda a: str(p.max_response), True),
            (p.error_query, lambda a: self._pop_error(), True),
            ("*IDN?", lambda a: self.idn, True),
            ("*RST", self._reset, False),
            ("*CLS", lambda a: self.errors.clear(), False),
            ("*OPC?", lambda a: "1", True),
            ("SYSTem:ERRor?", lambda a: self._pop_error(), True),
            ("SIMulation:MCU:SILent {mcu},{state}", self._set_silent, False),
            ("SIMulation:MCU:SILent? {mcu}", lambda a: "1" if self._mcu(a).silent else "0", True),
            ("SIMulation:MCU:LIST?", self._list_mcus, True),
        ]
        self.commands = [(CommandPattern(t), fn, replies) for t, fn, replies in commands if t]

    # --- entry point -------------------------------------------------------------------------

    def handle(self, line: str, client: str = "") -> bytes | None:
        """Run one command line. Returns the reply without its terminator, or None for no reply."""
        line = line.strip()
        if not line:
            return None
        with self.lock:
            reply = self._dispatch(line)
        log.debug("%s  %s  ->  %r", client, line, reply)
        return reply

    def _dispatch(self, line: str) -> bytes | None:
        replies = line.split(None, 1)[0].endswith("?")
        try:
            for pattern, fn, pattern_replies in self.commands:
                if not pattern.match_header(line):
                    continue
                replies = pattern_replies
                args = pattern.match(line)
                if args is None:
                    raise _syntax(f"expected {pattern.template}")
                result = fn(args)
                if result is None:
                    return None
                return result if isinstance(result, bytes) else result.encode("latin-1")
            raise _undefined_header(line.split(None, 1)[0])
        except ScpiError as e:
            self._push_error(e)
            if replies and self.error_reply:
                return str(e).encode("latin-1")
            return None

    # --- error queue -------------------------------------------------------------------------

    def _push_error(self, e: ScpiError) -> None:
        if len(self.errors) >= ERROR_QUEUE_SIZE:
            self.errors[-1] = ScpiError(-350, "Queue overflow")
            return
        self.errors.append(e)

    def _pop_error(self) -> str:
        return str(self.errors.popleft()) if self.errors else '0,"No error"'

    # --- parameters --------------------------------------------------------------------------

    def _int(self, args: dict, name: str, base: int = 0) -> int:
        try:
            return parse_int(args[name], base)
        except (ValueError, KeyError):
            raise _data_type(f"{name}={args.get(name)!r}") from None

    def _mcu(self, args: dict) -> Mcu:
        mcu = self.mcus.get(self._int(args, "mcu"))
        if mcu is None:
            raise _illegal_value(f"unknown MCU {args['mcu']}")
        return mcu

    def _reach(self, mcu: Mcu) -> None:
        """Model the main MCU asking a silent secondary MCU and giving up after its timeout."""
        if mcu.silent:
            time.sleep(self.internal_timeout_ms / 1000.0)
            raise _mcu_not_responding()

    # --- commands ----------------------------------------------------------------------------

    def _read(self, args: dict) -> bytes:
        mcu = self._mcu(args)
        addr = self._int(args, "addr", self._addr_base)
        size = self._int(args, "len")
        if size < 1:
            raise _illegal_value(f"len={size}")
        if size > self.profile.max_response:
            raise _too_much_data(f"len {size} > max {self.profile.max_response}")
        self._reach(mcu)
        try:
            data = mcu.read(addr, size)
        except MemoryFault as e:
            raise _out_of_range(str(e)) from None
        return encode_data(data, self.profile.data_format)

    def _write(self, args: dict) -> None:
        mcu = self._mcu(args)
        addr = self._int(args, "addr", self._addr_base)
        value = self._int(args, "value", self._value_base)
        size = self._int(args, "size") if "size" in args else self.profile.max_write
        if size not in WRITE_SIZES or size > self.profile.max_write:
            raise _illegal_value(f"size={size}")
        bits = 8 * size
        if not -(1 << (bits - 1)) <= value < (1 << bits):
            raise _out_of_range(f"value does not fit in {size} bytes")
        data = (value & ((1 << bits) - 1)).to_bytes(size, mcu.byte_order)
        self._reach(mcu)
        try:
            mcu.write(addr, data)
        except MemoryFault as e:
            raise _out_of_range(str(e)) from None

    def _reset(self, args: dict) -> None:
        for m in self.mcus.values():
            m.reset()
        self.errors.clear()

    def _set_silent(self, args: dict) -> None:
        mcu = self._mcu(args)
        state = args["state"].upper()
        if state not in ("0", "1", "ON", "OFF"):
            raise _illegal_value(f"state={args['state']}")
        if mcu.is_main:
            raise _illegal_value("the main MCU owns the link and cannot be silent")
        mcu.silent = state in ("1", "ON")

    def _list_mcus(self, args: dict) -> str:
        return ",".join(f'{m.id},"{m.name}"' for m in sorted(self.mcus.values(), key=lambda m: m.id))


def encode_data(data: bytes, fmt: str) -> bytes:
    """Encode a read reply, lowest address first.

    hex:        0000A041
    bytes_csv:  00,00,A0,41
    decimal:    0,0,160,65
    ieee_block: #14 followed by the 4 raw bytes (IEEE 488.2 definite-length block)
    """
    if fmt == "hex":
        return data.hex().upper().encode()
    if fmt == "bytes_csv":
        return ",".join(f"{b:02X}" for b in data).encode()
    if fmt == "decimal":
        return ",".join(str(b) for b in data).encode()
    if fmt == "ieee_block":
        n = str(len(data)).encode()
        return b"#" + str(len(n)).encode() + n + data
    raise ValueError(f"unknown data format {fmt!r}")
