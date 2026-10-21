"""SCPI protocol: turns memory reads and writes into the profile's commands and decodes replies.

This layer does one exchange at a time and has no retry policy; the session decides what a
timeout means and what to do next.
"""

from __future__ import annotations

import logging
import time

from ..errors import BadReply, CommandRejected
from ..profile import Profile, parse_error
from ..transport import Transport

traffic = logging.getLogger("rdt.traffic")


class ScpiProtocol:
    #: Longest wait, before the next command, for the late reply to a read that timed out (H1).
    #: Replies carry no tag, so a reply that comes after its read gave up would be taken as the
    #: next read's reply. The firmware answers late rather than never when it is busy, so it is
    #: waited for and dropped. But a failed command gets no reply at all, so every timeout costs
    #: this wait once more; a quarter second (or the MCU's own timeout, if shorter) catches a busy
    #: firmware and keeps a poll of a hung MCU close to its timeouts (rani, 2026-10-18, code review).
    LATE_REPLY_S = 0.25

    def __init__(self, transport: Transport, profile: Profile):
        self.transport = transport
        self.profile = profile
        self.term = profile.terminator.encode("latin-1")
        self.sep = self.term[-1:]  # CR LF replies also work with a "\n" terminator
        self._late_wait = 0.0      # > 0 while a reply may still come for a command that timed out

    # --- raw exchanges -----------------------------------------------------------------------

    def send(self, command: str) -> None:
        if self._late_wait:
            wait, self._late_wait = self._late_wait, 0.0
            late = self.transport.read_until(self.sep, time.monotonic() + wait)
            if late is not None:
                traffic.debug("   dropped late reply %r", late[:80])
        dropped = self.transport.discard_input()
        if dropped:
            traffic.debug("   dropped stale input %r", dropped[:80])
        traffic.debug("-> %s", command)
        self.transport.write(command.encode("latin-1") + self.term)

    def read_line(self, timeout: float) -> bytes | None:
        line = self.transport.read_until(self.sep, time.monotonic() + timeout)
        if line is not None:
            line = line.rstrip(b"\r")
            traffic.debug("<- %r", line[:200])
        return line

    def query(self, command: str, timeout: float) -> str | None:
        """Send a command and return its reply line, or None if nothing came back in time."""
        self.send(command)
        line = self.read_line(timeout)
        if line is None:
            self._owe_reply(timeout)
            return None
        return line.decode("latin-1")

    def _owe_reply(self, timeout: float) -> None:
        """A command got no reply in time; its reply may still come, so wait for it before the next."""
        self._late_wait = min(timeout, self.LATE_REPLY_S)

    # --- memory ------------------------------------------------------------------------------

    def read_mem(self, mcu: int, addr: int, length: int, timeout: float, byte_order: str | None = None) -> bytes | None:
        """One read command. None on timeout; ``BadReply`` if a reply came but doesn't fit."""
        self.send(self.profile.format_read(mcu, addr, length))
        reply = self.read_line(timeout)
        if reply is None:
            self._owe_reply(timeout)
            return None
        if reply[:1] == b"-":
            # A firmware that does reply with its error (the simulator's error_reply = true).
            # Data replies never start with "-", so the two can't be confused.
            try:
                error = parse_error(reply.decode("latin-1"))
            except BadReply:
                error = None
            if error is not None:
                raise CommandRejected(error)
        try:
            return self.profile.decode_data(reply, length, byte_order)
        except BadReply:
            # Perhaps the late reply to an earlier command, with this one's reply still to come.
            self._owe_reply(timeout)
            raise

    def write_mem(self, mcu: int, addr: int, value: int) -> None:
        """One write command. The firmware sends no reply to a write, success or failure."""
        self.send(self.profile.format_write(mcu, addr, value))

