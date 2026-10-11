"""Protocols: how memory accesses become bytes on the link. SCPI now, a binary protocol later."""

from .scpi import ScpiProtocol

__all__ = ["ScpiProtocol"]
