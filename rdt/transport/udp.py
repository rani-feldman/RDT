"""LAN UDP transport. A datagram can be lost, so the session re-sends reads that time out."""

from __future__ import annotations

import socket

from ..errors import LinkError
from .base import Transport, _broken


class UdpTransport(Transport):
    lossy = True

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None

    @property
    def description(self) -> str:
        return f"udp {self.host}:{self.port}"

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        self.close()
        try:
            info = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
            sock = socket.socket(info[0], socket.SOCK_DGRAM)
            # connect() makes the socket accept datagrams from the device only.
            sock.connect(info[4])
        except OSError as e:
            raise _broken(self.description, e) from None
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
            self._sock.send(data)
        except OSError as e:
            raise _broken(self.description, e) from None

    def _recv(self, timeout: float) -> bytes:
        if self._sock is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._sock.settimeout(timeout if timeout > 0 else 0.0)
            return self._sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return b""
        except ConnectionError:
            # An ICMP "port unreachable" from an earlier datagram. UDP has no connection to
            # lose, so this is just a request that got no answer.
            return b""
        except OSError as e:
            raise _broken(self.description, e) from None
