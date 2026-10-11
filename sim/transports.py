"""Transports the simulated device listens on: TCP, UDP and a serial port.

Each one only moves lines in and replies out; the device does the rest. Every TCP connection
and every UDP sender gets its own replies, so several tools can share the device.
"""

from __future__ import annotations

import logging
import os
import random
import socketserver
import sys
import threading

from .config import TransportConfig, parse_endpoint
from .device import Device

log = logging.getLogger("sim")


class LineSplitter:
    """Splits a byte stream into command lines on the terminator's last byte (CR LF also works)."""

    def __init__(self, terminator: str):
        self.sep = terminator.encode()[-1:]
        self.buf = b""

    def feed(self, data: bytes) -> list[str]:
        self.buf += data
        *lines, self.buf = self.buf.split(self.sep)
        return [line.decode("latin-1").strip("\r\n") for line in lines]


def _make_tcp_handler(device: Device):
    term = device.profile.terminator.encode()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            client = "tcp %s:%d" % self.client_address[:2]
            log.info("%s connected", client)
            lines = LineSplitter(device.profile.terminator)
            try:
                while data := self.request.recv(4096):
                    for line in lines.feed(data):
                        reply = device.handle(line, client)
                        if reply is not None:
                            self.request.sendall(reply + term)
            except OSError:
                pass
            log.info("%s disconnected", client)

    return Handler


def _make_udp_handler(device: Device, drop: float, rng: random.Random):
    term = device.profile.terminator.encode()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            data, sock = self.request
            client = "udp %s:%d" % self.client_address[:2]
            if drop and rng.random() < drop:
                log.debug("%s  dropped %r", client, data)
                return
            lines = LineSplitter(device.profile.terminator)
            for line in lines.feed(data) + [lines.buf.decode("latin-1")]:
                reply = device.handle(line, client)
                if reply is not None:
                    sock.sendto(reply + term, self.client_address)

    return Handler


# On Windows SO_REUSEADDR would let a second simulator silently share the port, so it stays off.
_REUSE = sys.platform != "win32"


class _TcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = _REUSE
    daemon_threads = True


class _UdpServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = _REUSE
    daemon_threads = True


class SerialServer:
    """Serves the device on a serial port: a real or virtual COM port, or a Linux pseudo-terminal."""

    def __init__(self, device: Device, port: str, baud: int):
        self.device = device
        self.stop_event = threading.Event()
        self._master = self._slave = None
        self._serial = None
        if port == "pty":
            if sys.platform == "win32":
                raise ValueError('serial "pty" works on Linux only; on Windows use a com0com pair')
            import tty

            self._master, self._slave = os.openpty()
            tty.setraw(self._slave)
            self.port = os.ttyname(self._slave)
        else:
            import serial

            self._serial = serial.Serial(port, baud, timeout=0.1)
            self.port = port
        self.thread = threading.Thread(target=self._run, name="sim-serial", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _read(self) -> bytes:
        if self._serial is not None:
            return self._serial.read(4096)
        import select

        ready, _, _ = select.select([self._master], [], [], 0.1)
        return os.read(self._master, 4096) if ready else b""

    def _write(self, data: bytes) -> None:
        if self._serial is not None:
            self._serial.write(data)
        else:
            os.write(self._master, data)

    def _run(self) -> None:
        term = self.device.profile.terminator.encode()
        lines = LineSplitter(self.device.profile.terminator)
        client = f"serial {self.port}"
        while not self.stop_event.is_set():
            try:
                data = self._read()
            except OSError:
                break
            for line in lines.feed(data):
                reply = self.device.handle(line, client)
                if reply is not None:
                    self._write(reply + term)

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1)
        if self._serial is not None:
            self._serial.close()
        for fd in (self._master, self._slave):
            if fd is not None:
                os.close(fd)


class Simulator:
    """Runs the device on every configured transport until ``stop``."""

    def __init__(self, device: Device, transport: TransportConfig, seed: int | None = None):
        self.device = device
        self.transport = transport
        self.rng = random.Random(seed)
        self.tcp_address: tuple[str, int] | None = None
        self.udp_address: tuple[str, int] | None = None
        self.serial_port: str | None = None
        self._servers: list[socketserver.BaseServer] = []
        self._serial: SerialServer | None = None

    def start(self) -> Simulator:
        t = self.transport
        if t.tcp:
            srv = _TcpServer(parse_endpoint(t.tcp), _make_tcp_handler(self.device))
            self.tcp_address = srv.server_address[:2]
            self._serve(srv)
        if t.udp:
            srv = _UdpServer(parse_endpoint(t.udp), _make_udp_handler(self.device, t.udp_drop, self.rng))
            self.udp_address = srv.server_address[:2]
            self._serve(srv)
        if t.serial:
            self._serial = SerialServer(self.device, t.serial, t.baud)
            self._serial.start()
            self.serial_port = self._serial.port
        return self

    def _serve(self, srv: socketserver.BaseServer) -> None:
        self._servers.append(srv)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def stop(self) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        self._servers.clear()
        if self._serial is not None:
            self._serial.stop()
            self._serial = None

    def __enter__(self) -> Simulator:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
