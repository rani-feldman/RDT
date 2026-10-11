"""Transport plug-in interface: moves bytes to and from the device, nothing more.

A transport knows nothing about SCPI. It opens, closes, writes bytes, and reads whatever has
arrived. Line and block framing is built on top of ``_recv`` here, so each plug-in only has to
say how to receive some bytes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from ..errors import LinkError


class Transport(ABC):
    #: Whether the medium keeps message boundaries and can lose a request (UDP).
    lossy = False

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable endpoint, such as ``tcp 192.168.1.20:5025`` or ``serial COM12 115200``."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def open(self) -> None:
        """Open the link. Raises ``LinkError`` if the device can't be reached."""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send ``data``. Raises ``LinkError`` if the link broke."""

    @abstractmethod
    def _recv(self, timeout: float) -> bytes:
        """Return what arrives within ``timeout`` seconds (0 = only what's already there), or b"".

        Raises ``LinkError`` if the link broke.
        """

    # --- framing helpers ---------------------------------------------------------------------

    def discard_input(self) -> bytes:
        """Drop everything received so far, such as a late reply to a command that timed out."""
        dropped = bytes(self._buf)
        self._buf.clear()
        while chunk := self._recv(0):
            dropped += chunk
        return dropped

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        chunk = self._recv(remaining)
        self._buf += chunk
        return bool(chunk)

    def read_until(self, sep: bytes, deadline: float) -> bytes | None:
        """Read up to and including ``sep``; returns the bytes before it, or None on timeout."""
        while True:
            i = self._buf.find(sep)
            if i >= 0:
                line = bytes(self._buf[:i])
                del self._buf[:i + len(sep)]
                return line
            if not self._fill(deadline):
                if time.monotonic() >= deadline:
                    return None

    def read_exact(self, n: int, deadline: float) -> bytes | None:
        while len(self._buf) < n:
            if not self._fill(deadline) and time.monotonic() >= deadline:
                return None
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def peek_byte(self, deadline: float) -> int | None:
        while not self._buf:
            if not self._fill(deadline) and time.monotonic() >= deadline:
                return None
        return self._buf[0]

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _broken(desc: str, e: OSError) -> LinkError:
    return LinkError(f"{desc}: {e.strerror or e}")
