"""LAN TCP transport: one connection per tool, so the firmware replies on the connection that asked."""

from __future__ import annotations

import socket

from ..errors import LinkError
from .base import Transport, _broken


class TcpTransport(Transport):
    def __init__(self, host: str, port: int, connect_timeout: float = 3.0):
        super().__init__()
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None

    @property
    def description(self) -> str:
        return f"tcp {self.host}:{self.port}"

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        self.close()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as e:
            raise _broken(self.description, e) from None
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._buf.clear()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._sock.settimeout(self.connect_timeout)
            self._sock.sendall(data)
        except OSError as e:
            self.close()
            raise _broken(self.description, e) from None

    def check(self) -> None:
        """Peek without waiting: a closed connection reads as end of file, a reset as an error."""
        if self._sock is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._sock.settimeout(0.0)
            data = self._sock.recv(1, socket.MSG_PEEK)
        except (socket.timeout, BlockingIOError):
            return
        except OSError as e:
            self.close()
            raise _broken(self.description, e) from None
        if not data:
            self.close()
            raise LinkError(f"{self.description}: the device closed the connection")

    def _recv(self, timeout: float) -> bytes:
        if self._sock is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._sock.settimeout(timeout if timeout > 0 else 0.0)
            data = self._sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return b""
        except OSError as e:
            self.close()
            raise _broken(self.description, e) from None
        if not data:
            self.close()
            raise LinkError(f"{self.description}: the device closed the connection")
        return data
