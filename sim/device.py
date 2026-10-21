"""The simulated device: the main MCU's SCPI command handler.

Commands from every transport and client go through one lock, as the firmware's single SCPI
parser would handle them one at a time. The SCPI error queue is shared by all clients.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque

from .mcu import Mcu
from .memory import MemoryFault
from .profile import WRITE_SIZES, CommandPattern, Profile, parse_hex

log = logging.getLogger("sim")

ERROR_QUEUE_SIZE = 16
_DECIMAL = re.compile(r"[0-9]+")
_ADDRESS = re.compile(r"[0-9A-Fa-f]{8}")


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
        self.frozen = False          # a hung board: nothing answers, the port stays open
        self.frozen_until = 0.0      # set by SIMulation:FREEze {seconds}

        p = self.profile
        # (template, handler, replies). Profile templates come first, so a user command wins over
        # a built-in with the same header. The read always replies, even if its header has no "?".
        commands = [
            (p.read, self._read, True),
            (p.write, self._write, False),
            (p.error_query, lambda a: self._pop_error(), True),
            ("*IDN?", lambda a: self.idn, True),
            ("*RST", self._reset, False),
            ("*CLS", lambda a: self.errors.clear(), False),
            ("*OPC?", lambda a: "1", True),
            ("SYSTem:ERRor?", lambda a: self._pop_error(), True),
            ("SIMulation:MCU:SILent {mcu},{state}", self._set_silent, False),
            ("SIMulation:MCU:SILent? {mcu}", lambda a: "1" if self._mcu(a).silent else "0", True),
            ("SIMulation:MCU:LIST?", self._list_mcus, True),
            ("SIMulation:FREEze {seconds}", self._freeze, False),
        ]
        self.commands = [(CommandPattern(t), fn, replies) for t, fn, replies in commands if t]

    # --- entry point -------------------------------------------------------------------------

    def handle(self, line: str, client: str = "") -> bytes | None:
        """Run one command line. Returns the reply without its terminator, or None for no reply."""
        line = line.strip()
        if not line:
            return None
        if self.frozen or time.monotonic() < self.frozen_until:
            log.debug("%s  %s  ->  (frozen, no reply)", client, line)
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

    def _int(self, args: dict, name: str) -> int:
        """An MCU, length or size: plain decimal, as the firmware takes it. ``0x10``, ``#H10``,
        ``+4`` and ``1_6`` fail like any other bad parameter (rani, 2026-10-18, code review)."""
        text = args.get(name)
        if text is None or not _DECIMAL.fullmatch(text.strip()):
            raise _data_type(f"{name}={text!r} is not decimal")
        return int(text)

    def _addr(self, args: dict) -> int:
        """An address: exactly 8 bare hex digits, zero-padded, as the firmware takes it. A shorter
        or longer one gets no reply, so the simulator catches a host that stops padding
        (rani, 2026-10-18, code review)."""
        text = args.get("addr")
        if text is None or not _ADDRESS.fullmatch(text.strip()):
            raise _data_type(f"addr={text!r} is not 8 bare hex digits")
        return int(text, 16)

    def _hex(self, args: dict, name: str) -> int:
        """An address or value: bare hex with no prefix, as the firmware takes it."""
        try:
            return parse_hex(args[name])
        except (ValueError, KeyError):
            raise _data_type(f"{name}={args.get(name)!r} is not bare hex") from None

    def _mcu(self, args: dict) -> Mcu:
        mcu = self.mcus.get(self._int(args, "mcu"))
        if mcu is None:
            raise _illegal_value(f"unknown MCU {args['mcu']}")
        return mcu

    def _aligned(self, addr: int, size: int) -> None:
        """The firmware takes only whole aligned words: address and length multiples of align."""
        align = self.profile.align
        if addr % align or size % align:
            raise _illegal_value(f"addr {addr:X} and size {size} must be multiples of {align}")

    def _reach(self, mcu: Mcu) -> None:
        """Model the main MCU asking a silent secondary MCU and giving up after its timeout."""
        if mcu.silent:
            time.sleep(self.internal_timeout_ms / 1000.0)
            raise _mcu_not_responding()

    # --- commands ----------------------------------------------------------------------------

    def _read(self, args: dict) -> bytes:
        mcu = self._mcu(args)
        addr = self._addr(args)
        size = self._int(args, "len")
        if size < 1:
            raise _illegal_value(f"len={size}")
        if size > self.profile.max_response:
            raise _too_much_data(f"len {size} > max {self.profile.max_response}")
        self._aligned(addr, size)
        self._reach(mcu)
        try:
            data = mcu.read(addr, size)
        except MemoryFault as e:
            raise _out_of_range(str(e)) from None
        return encode_data(data, self.profile.align, mcu.byte_order)

    def _write(self, args: dict) -> None:
        mcu = self._mcu(args)
        addr = self._addr(args)
        value = self._hex(args, "value")
        size = self._int(args, "size") if "size" in args else self.profile.max_write
        if size not in WRITE_SIZES or size > self.profile.max_write:
            raise _illegal_value(f"size={size}")
        self._aligned(addr, size)
        bits = 8 * size
        if value >= 1 << bits:
            raise _out_of_range(f"value does not fit in {size} bytes")
        data = value.to_bytes(size, mcu.byte_order)
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

    def _freeze(self, args: dict) -> None:
        """Answer nothing for a while, like a hung board whose COM port stays open."""
        try:
            seconds = float(args["seconds"])
        except ValueError:
            raise _data_type(args["seconds"]) from None
        self.frozen_until = time.monotonic() + seconds

    def _list_mcus(self, args: dict) -> str:
        return ",".join(f'{m.id},"{m.name}"' for m in sorted(self.mcus.values(), key=lambda m: m.id))


def encode_data(data: bytes, size: int = 4, byte_order: str = "little") -> bytes:
    """A read reply in words: one bare hex value per ``size``-byte word, comma-separated, such as
    ``3FC00000,00000101``, the way a firmware that prints ``uint32_t`` values sends it."""
    return ",".join(f"{int.from_bytes(data[i:i + size], byte_order):0{2 * size}X}"
                    for i in range(0, len(data), size)).encode()
