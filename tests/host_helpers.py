"""Helpers for the host tests: a simulated device on free ports, a target pointed at it, and a
TCP relay that can be cut like a pulled cable."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from rdt import ConnectionConfig, McuConfig, Profile, Target, TargetConfig

FIRMWARE = Path(__file__).resolve().parent.parent / "sim" / "examples" / "firmware"


def host_mcus(**timeouts):
    return [McuConfig(0, "main", FIRMWARE / "main.elf", timeouts.get("main")),
            McuConfig(1, "motorctl", FIRMWARE / "motorctl.elf", timeouts.get("motorctl"))]


def make_target(kind, address, profile=None, mcus=None, **kw) -> Target:
    if not isinstance(address, str):
        address = "%s:%d" % tuple(address)
    cfg = TargetConfig(ConnectionConfig(kind, address), profile or Profile(timeout_ms=300), mcus or host_mcus())
    return Target(cfg, **kw)


class Relay:
    """Forwards TCP connections to the simulator. ``cut()`` drops every connection and refuses
    new ones (accepts and closes them at once), until ``heal()``."""

    def __init__(self, upstream):
        self.upstream = tuple(upstream)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.address = self.listener.getsockname()[:2]
        self.broken = False
        self.conns: list[socket.socket] = []
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if self.broken:
                client.close()
                continue
            up = socket.create_connection(self.upstream)
            with self.lock:
                self.conns += [client, up]
            for a, b in ((client, up), (up, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cut(self):
        self.broken = True
        with self.lock:
            for s in self.conns:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            self.conns.clear()

    def heal(self):
        self.broken = False

    def close(self):
        self.running = False
        self.cut()
        self.listener.close()
