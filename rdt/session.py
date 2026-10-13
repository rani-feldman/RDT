"""Session: sequencing, timeouts, retries, silent MCUs and reconnecting.

The session is where the firmware's habits are handled:
- A failed command sends no reply, so a timeout may be an error. After a read times out, the
  error query is sent once and its answer kept as a hint. The queue is shared with other tools
  on the link, so the hint is never relied on.
- A write sends no reply either way, so every write is confirmed by reading the value back.
- Every read and write covers whole aligned words (the profile's ``align``, 4 by default). A read
  of 2 bytes reads the 4-byte word and keeps the 2 bytes asked for. A write smaller than a word,
  or not on a word boundary, reads the word, replaces the bytes and writes the whole word back
  (read-modify-write). A neighbouring value the firmware changes in between is overwritten.
- A secondary MCU that stops answering is marked silent: on the -240 error, or after three
  timeouts in a row while the main MCU still answers. Only that MCU's accesses fail, and it is
  probed again every few seconds.
- When the link breaks or the device stops answering, the session goes down, refuses accesses,
  and reconnects in the background with a growing delay.

One lock serialises every exchange, so the GUI's poller and its console can share a session.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .errors import (
    BadReply, CommandRejected, CommandTimeout, DeviceError, LinkDown, LinkError, McuSilent, WriteNotConfirmed,
)
from .profile import Profile, align_span
from .protocol import ScpiProtocol
from .transport import Transport

log = logging.getLogger("rdt.session")

MCU_NOT_RESPONDING = -240


class LinkState(Enum):
    CLOSED = "closed"
    UP = "up"
    DOWN = "down"


@dataclass
class McuStatus:
    silent: bool = False
    timeouts_in_row: int = 0
    last_probe: float = 0.0


Listener = Callable[[str, dict], None]


class Session:
    SILENT_AFTER = 3            # timeouts in a row, while MCU 0 answers, that mark an MCU silent
    SILENT_PROBE_S = 2.0        # how often a silent MCU is tried again
    RECONNECT_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 5.0)
    RETRY_DELAY_S = 0.05        # first delay before re-sending a timed-out read; doubles each time

    def __init__(
        self,
        transport: Transport,
        profile: Profile,
        *,
        mcu_timeouts_ms: dict[int, float] | None = None,
        mcu_names: dict[int, str] | None = None,
        byte_order_of: Callable[[int], str] | None = None,
        auto_reconnect: bool = True,
    ):
        self.transport = transport
        self.profile = profile
        self.protocol = ScpiProtocol(transport, profile)
        self.mcu_timeouts_ms = dict(mcu_timeouts_ms or {})
        self.mcu_names = dict(mcu_names or {})
        self.byte_order_of = byte_order_of or (lambda mcu: profile.byte_order)   # for word replies
        self.auto_reconnect = auto_reconnect
        self.max_response = profile.max_response
        self.idn: str | None = None
        self.state = LinkState.CLOSED
        self.down_reason = ""
        self.last_hint: DeviceError | None = None
        self._mcus: dict[int, McuStatus] = {}
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._reconnector: threading.Thread | None = None

    # --- events ------------------------------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        """``fn(event, info)`` is called on ``"link"`` (state changes) and ``"mcu"`` (silent or back)."""
        self._listeners.append(fn)

    def _emit(self, event: str, **info) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, info)
            except Exception:
                log.exception("session listener failed")

    def _set_state(self, state: LinkState, reason: str = "") -> None:
        if state != self.state:
            self.state = state
            self.down_reason = reason
            log.info("link %s%s", state.value, f": {reason}" if reason else "")
            self._emit("link", state=state, reason=reason)

    # --- connect and reconnect ---------------------------------------------------------------

    @property
    def is_up(self) -> bool:
        return self.state is LinkState.UP

    def connect(self) -> None:
        """Open the link and check that the device answers. Raises ``LinkError`` if it doesn't."""
        self._stop.clear()
        with self._lock:
            self._open()
            self._set_state(LinkState.UP)

    def _open(self) -> None:
        p = self.profile
        self.transport.open()
        try:
            if p.probe:
                reply = self._query_with_retries(p.probe)
                if reply is None:
                    raise LinkError(f"{self.transport.description}: no reply to {p.probe}")
                self.idn = reply
            self.max_response = p.max_response
        except LinkError:
            self.transport.close()
            raise
        for st in self._mcus.values():
            st.timeouts_in_row = 0

    def _query_with_retries(self, command: str) -> str | None:
        attempts = 1 + (self.profile.retries if self.transport.lossy else 0)
        for _ in range(attempts):
            reply = self.protocol.query(command, self.timeout_s(0))
            if reply is not None:
                return reply
        return None

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self.transport.close()
            self._set_state(LinkState.CLOSED)
        t = self._reconnector
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    def _link_lost(self, reason: str) -> LinkDown:
        """Mark the link down, start reconnecting, and return the error to raise."""
        self.transport.close()
        self._set_state(LinkState.DOWN, reason)
        if self.auto_reconnect and not self._stop.is_set():
            if self._reconnector is None or not self._reconnector.is_alive():
                self._reconnector = threading.Thread(target=self._reconnect_loop, name="rdt-reconnect", daemon=True)
                self._reconnector.start()
        return LinkDown(f"link down: {reason}")

    def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            delay = self.RECONNECT_DELAYS[min(attempt, len(self.RECONNECT_DELAYS) - 1)]
            if self._stop.wait(delay):
                return
            attempt += 1
            with self._lock:
                if self.state is not LinkState.DOWN:
                    return
                try:
                    self._open()
                except LinkError as e:
                    log.debug("reconnect attempt %d failed: %s", attempt, e)
                    continue
                self._set_state(LinkState.UP)
                return

    def wait_until_up(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.is_up:
            if self.state is LinkState.CLOSED or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def _require_up(self) -> None:
        if self.state is LinkState.CLOSED:
            raise LinkDown("not connected")
        if self.state is LinkState.DOWN:
            raise LinkDown(f"link down: {self.down_reason}")

    # --- MCUs --------------------------------------------------------------------------------

    def timeout_s(self, mcu: int) -> float:
        return self.mcu_timeouts_ms.get(mcu, self.profile.timeout_ms) / 1000.0

    def _name(self, mcu: int) -> str:
        return f"MCU {self.mcu_names[mcu]}" if mcu in self.mcu_names else f"MCU {mcu}"

    def mcu_status(self, mcu: int) -> McuStatus:
        return self._mcus.setdefault(mcu, McuStatus())

    def is_silent(self, mcu: int) -> bool:
        return self.mcu_status(mcu).silent

    def _mark_silent(self, mcu: int, why: str) -> McuSilent:
        st = self.mcu_status(mcu)
        if not st.silent:
            st.silent = True
            log.info("MCU %d silent: %s", mcu, why)
            self._emit("mcu", mcu=mcu, silent=True, reason=why)
        st.last_probe = time.monotonic()
        return McuSilent(f"{self._name(mcu)} is not responding ({why})")

    def _mcu_answered(self, mcu: int) -> None:
        st = self.mcu_status(mcu)
        st.timeouts_in_row = 0
        if st.silent:
            st.silent = False
            log.info("MCU %d answers again", mcu)
            self._emit("mcu", mcu=mcu, silent=False, reason="")

    # --- reads -------------------------------------------------------------------------------

    def read(self, mcu: int, addr: int, size: int) -> bytes:
        """Read ``size`` bytes. The device is asked for whole aligned words, in pieces of at most
        ``max_response`` bytes, and the bytes asked for are cut out of the result."""
        if size < 1:
            raise ValueError("size must be at least 1")
        align = self.profile.align
        start, end = align_span(addr, size, align)
        chunk = self.max_response - self.max_response % align
        out = bytearray()
        with self._lock:
            for piece in range(start, end, chunk):
                out += self._read_piece(mcu, piece, min(chunk, end - piece))
        return bytes(out[addr - start:addr - start + size])

    def _read_piece(self, mcu: int, addr: int, size: int) -> bytes:
        self._require_up()
        st = self.mcu_status(mcu)
        if st.silent:
            if time.monotonic() - st.last_probe < self.SILENT_PROBE_S:
                raise McuSilent(f"{self._name(mcu)} is not responding; retried every {self.SILENT_PROBE_S:g} s")
            st.last_probe = time.monotonic()
            attempts = 1
        else:
            attempts = 1 + self.profile.retries
        what = f"read of {size} bytes at 0x{addr:08X} on {self._name(mcu)}"
        hint: DeviceError | None = None
        asked = main_answered = False
        bad: BadReply | None = None
        delay = self.RETRY_DELAY_S
        try:
            for attempt in range(attempts):
                if attempt:
                    time.sleep(delay)
                    delay *= 2
                try:
                    data = self.protocol.read_mem(mcu, addr, size, self.timeout_s(mcu),
                                                  self.byte_order_of(mcu))
                except BadReply as e:
                    # Perhaps a late reply meant for an earlier command; the next send flushes it.
                    log.debug("%s: %s", what, e)
                    bad = e
                    main_answered = True
                    continue
                except CommandRejected as e:
                    self.last_hint = e.error
                    if mcu != 0 and e.error.code == MCU_NOT_RESPONDING:
                        raise self._mark_silent(mcu, str(e.error)) from None
                    raise
                if data is not None:
                    self._mcu_answered(mcu)
                    return data
                if not asked:
                    asked = True
                    answered, hint = self._error_query()
                    main_answered |= answered
                    if hint is not None:
                        self.last_hint = hint
                if mcu != 0 and main_answered:
                    if hint is not None and hint.code == MCU_NOT_RESPONDING:
                        raise self._mark_silent(mcu, str(hint))
                    st.timeouts_in_row += 1
                    if st.timeouts_in_row >= self.SILENT_AFTER:
                        raise self._mark_silent(mcu, f"{st.timeouts_in_row} timeouts in a row")
        except LinkError as e:
            raise self._link_lost(str(e)) from None
        if not main_answered:
            raise self._link_lost(f"the device stopped answering ({what} and {self.profile.error_query} got no reply)")
        if hint is not None:
            st.timeouts_in_row = 0  # the failure is explained, so it says nothing about a silent MCU
        if bad is not None and hint is None:
            raise BadReply(f"{what}: {bad}")
        raise CommandTimeout(f"no reply to {what}", hint)

    def _error_query(self) -> tuple[bool, DeviceError | None]:
        attempts = 1 + (self.profile.retries if self.transport.lossy else 0)
        for _ in range(attempts):
            answered, error = self.protocol.error_query(self.timeout_s(0))
            if answered:
                return True, error
        return False, None

    def error_query(self) -> DeviceError | None:
        """Read one entry of the shared error queue (None for "No error")."""
        with self._lock:
            self._require_up()
            try:
                answered, error = self._error_query()
            except LinkError as e:
                raise self._link_lost(str(e)) from None
            if not answered:
                raise CommandTimeout(f"no reply to {self.profile.error_query}")
            return error

    # --- writes ------------------------------------------------------------------------------

    def write(self, mcu: int, addr: int, data: bytes, byte_order: str = "little") -> None:
        """Write ``data`` one value at a time and confirm it by reading it back.

        Every write covers whole aligned words. When ``data`` does not, the words are read first
        and ``data`` is put into them (read-modify-write). Values wider than ``max_write`` are
        split. A multi-piece write is not atomic.
        """
        if not data:
            raise ValueError("nothing to write")
        start, end = align_span(addr, len(data), self.profile.align)
        off = addr - start
        with self._lock:
            self._require_up()
            if self.is_silent(mcu):
                raise McuSilent(f"{self._name(mcu)} is not responding; write refused")
            if (start, end) == (addr, addr + len(data)):
                words = data
            else:
                words = self._patch(self._read_words(mcu, addr, data, start, end), off, data)
            pieces = split_write(start, words, self.profile.max_write)
            try:
                self._send_pieces(mcu, pieces, byte_order)
            except LinkError as e:
                raise self._link_lost(str(e)) from None
            words_back = self._read_back(mcu, addr, data, start, end)
            back = words_back[off:off + len(data)]
            # Over UDP a write datagram can be lost. It is re-sent only after the read-back
            # showed it did not land, never blindly. Bytes around the value are taken from the
            # latest read-back, so a re-send does not undo a change the firmware made to them.
            tries = self.profile.retries if self.transport.lossy else 0
            while back != data and tries > 0:
                tries -= 1
                words = self._patch(words_back, off, data)
                wrong = [(a, words[a - start:a - start + len(chunk)]) for a, chunk in pieces
                         if words_back[a - start:a - start + len(chunk)] != words[a - start:a - start + len(chunk)]]
                try:
                    self._send_pieces(mcu, wrong, byte_order)
                except LinkError as e:
                    raise self._link_lost(str(e)) from None
                words_back = self._read_back(mcu, addr, data, start, end)
                back = words_back[off:off + len(data)]
            if back == data:
                return
            try:
                _, hint = self._error_query()
            except LinkError as e:
                raise self._link_lost(str(e)) from None
            if hint is not None:
                self.last_hint = hint
            raise WriteNotConfirmed(
                f"write to 0x{addr:08X} on {self._name(mcu)} not confirmed: wrote {data.hex().upper()}, "
                f"read back {back.hex().upper()}", data, back, hint)

    @staticmethod
    def _patch(words: bytes, off: int, data: bytes) -> bytes:
        return words[:off] + data + words[off + len(data):]

    def _read_words(self, mcu: int, addr: int, data: bytes, start: int, end: int) -> bytes:
        """Read the words a read-modify-write puts ``data`` into."""
        try:
            return self.read(mcu, start, end - start)
        except (CommandTimeout, BadReply, McuSilent, CommandRejected) as e:
            raise WriteNotConfirmed(f"write to 0x{addr:08X} on {self._name(mcu)} not sent: reading the "
                                    f"{self.profile.align}-byte words around it failed: {e}",
                                    data, None, getattr(e, "hint", None)) from None

    def _send_pieces(self, mcu: int, pieces: list[tuple[int, bytes]], byte_order: str) -> None:
        for a, chunk in pieces:
            self.protocol.write_mem(mcu, a, int.from_bytes(chunk, byte_order), len(chunk))

    def _read_back(self, mcu: int, addr: int, data: bytes, start: int, end: int) -> bytes:
        try:
            return self.read(mcu, start, end - start)
        except (CommandTimeout, BadReply, McuSilent, CommandRejected) as e:
            raise WriteNotConfirmed(f"write to 0x{addr:08X} on {self._name(mcu)} sent, but the read-back failed: {e}",
                                    data, None, getattr(e, "hint", None)) from None

    # --- console -----------------------------------------------------------------------------

    def send_break(self, ms: float = 250) -> None:
        """Send a serial break (COM port only). Raises LinkError on another transport."""
        with self._lock:
            self._require_up()
            self.transport.send_break(ms / 1000.0)

    def command(self, text: str, expect_reply: bool | None = None, timeout_ms: float | None = None) -> str | None:
        """Send any command by hand. Waits for a reply if the header ends in ``?`` (or if asked to).

        Returns the reply, or None if none came. A command that fails sends no reply, so ask the
        error query to find out why.
        """
        text = text.strip()
        if not text:
            raise ValueError("empty command")
        if expect_reply is None:
            expect_reply = text.split(None, 1)[0].endswith("?")
        timeout = (timeout_ms / 1000.0) if timeout_ms else self.timeout_s(0)
        with self._lock:
            self._require_up()
            try:
                if expect_reply:
                    return self.protocol.query(text, timeout)
                self.protocol.send(text)
                return None
            except LinkError as e:
                raise self._link_lost(str(e)) from None


def split_write(addr: int, data: bytes, max_write: int) -> list[tuple[int, bytes]]:
    """Split a write into naturally aligned pieces of 1, 2, 4 or 8 bytes, none wider than ``max_write``."""
    pieces = []
    pos = 0
    while pos < len(data):
        a = addr + pos
        size = max_write
        while size > 1 and (a % size or size > len(data) - pos):
            size //= 2
        pieces.append((a, data[pos:pos + size]))
        pos += size
    return pieces
