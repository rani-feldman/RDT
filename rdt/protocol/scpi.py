"""SCPI protocol: turns memory reads and writes into the profile's commands and decodes replies.

This layer does one exchange at a time and has no retry policy; the session decides what a
timeout means and what to do next.
"""

from __future__ import annotations

import logging
import time

from ..errors import BadReply, CommandRejected, DeviceError
from ..profile import Profile, parse_error
from ..transport import Transport

traffic = logging.getLogger("rdt.traffic")


class ScpiProtocol:
    def __init__(self, transport: Transport, profile: Profile):
        self.transport = transport
        self.profile = profile
        self.term = profile.terminator.encode("latin-1")
        self.sep = self.term[-1:]  # CR LF replies also work with a "\n" terminator

    # --- raw exchanges -----------------------------------------------------------------------

    def send(self, command: str) -> None:
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
        return None if line is None else line.decode("latin-1")

    # --- memory ------------------------------------------------------------------------------

    def read_mem(self, mcu: int, addr: int, length: int, timeout: float, byte_order: str | None = None) -> bytes | None:
        """One read command. None on timeout; ``BadReply`` if a reply came but doesn't fit."""
        self.send(self.profile.format_read(mcu, addr, length))
        reply = self.read_line(timeout)
        if reply is None:
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
        return self.profile.decode_data(reply, length, byte_order)

    def write_mem(self, mcu: int, addr: int, value: int, size: int) -> None:
        """One write command. The firmware sends no reply to a write, success or failure."""
        self.send(self.profile.format_write(mcu, addr, value, size))

    # --- error queue -------------------------------------------------------------------------

    def error_query(self, timeout: float) -> tuple[bool, DeviceError | None]:
        """Ask the error queue once. Returns (answered, error); error is None for "No error".

        The queue is shared with other tools on the link, so the answer is only a hint.
        """
        if not self.profile.error_query:
            return True, None
        reply = self.query(self.profile.error_query, timeout)
        if reply is None:
            return False, None
        try:
            return True, parse_error(reply)
        except BadReply:
            return True, None
