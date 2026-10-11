"""Transports: plug-in byte movers. COM port, LAN TCP and LAN UDP for now."""

from __future__ import annotations

from ..errors import ConfigError
from .base import Transport
from .serial import SerialTransport
from .tcp import TcpTransport
from .udp import UdpTransport

KINDS = ("tcp", "udp", "serial")


def parse_endpoint(text: str, default_port: int = 5025) -> tuple[str, int]:
    """``HOST:PORT``, ``HOST`` or ``:PORT``; IPv6 hosts go in brackets, as in ``[::1]:5025``."""
    text = text.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, sep, port = text.rpartition(":")
        if not sep:
            host, port = text, ""
    try:
        return host or "127.0.0.1", int(port) if port else default_port
    except ValueError:
        raise ConfigError(f"bad endpoint {text!r}: expected HOST:PORT") from None


def make_transport(kind: str, address: str, baud: int = 115200) -> Transport:
    """``make_transport("tcp", "192.168.1.20:5025")`` or ``make_transport("serial", "COM12", 115200)``."""
    if kind == "tcp":
        return TcpTransport(*parse_endpoint(address))
    if kind == "udp":
        return UdpTransport(*parse_endpoint(address))
    if kind == "serial":
        return SerialTransport(address, baud)
    raise ConfigError(f"transport must be one of {', '.join(KINDS)}, not {kind!r}")


__all__ = ["KINDS", "SerialTransport", "TcpTransport", "Transport", "UdpTransport", "make_transport", "parse_endpoint"]
