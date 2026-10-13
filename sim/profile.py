"""SCPI profile: the user-defined command templates, and matching incoming lines against them.

The simulator reads the same ``[scpi]`` table the host uses, so whatever templates the user
defines are exactly the commands the simulated device answers. As in the firmware, addresses
and values are bare hex with no prefix (``20000000``), and a read reply is one bare hex value
per aligned word, comma-separated (``3FC00000,00000101``).
Every read and write must be whole aligned words (``align``, 4 bytes by default).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{(\w+)(?::[^}]*)?\}")


@dataclass
class Profile:
    name: str = "Default"
    read: str = "MEMory:READ? {mcu},{addr},{len}"
    write: str = "MEMory:WRITE {mcu},{addr},{value},{size}"
    max_write: int = 4
    align: int = 4          # addr, len and size must be multiples of this (rani, 2026-10-12)
    max_response: int = 1024
    error_query: str = "SYSTem:ERRor?"
    terminator: str = "\n"

    def __post_init__(self) -> None:
        if self.max_response < 1:
            raise ValueError("max_response must be at least 1")
        if self.align not in (1, 2, 4, 8):
            raise ValueError("align must be 1, 2, 4 or 8")
        if not self.terminator:
            raise ValueError("terminator must not be empty")

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f).get("scpi", {}))

    @classmethod
    def from_dict(cls, table: dict) -> Profile:
        # Host-only keys (byte_order, timeout_ms, ...) are ignored here.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in table.items() if k in known})


def _header_node_regex(node: str) -> str:
    """One colon-separated header node. ``MEMory`` accepts ``MEM`` or ``MEMORY``, any case."""
    query = node.endswith("?")
    if query:
        node = node[:-1]
    has_upper = any(c.isupper() for c in node)
    has_lower = any(c.islower() for c in node)
    if has_upper and has_lower:
        short = "".join(c for c in node if not c.islower())
        body = f"(?:{re.escape(node.upper())}|{re.escape(short)})"
    else:
        body = re.escape(node)
    return body + (r"\?" if query else "")


def _header_regex(header: str) -> str:
    # A leading colon is optional in SCPI, whether or not the template has one.
    nodes = header.lstrip(":").split(":")
    return ":?" + ":".join(_header_node_regex(n) for n in nodes)


def _args_regex(args: str) -> str:
    out = []
    pos = 0
    for m in _PLACEHOLDER.finditer(args):
        out.append(_literal_regex(args[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>[^\\s,]+)")
        pos = m.end()
    out.append(_literal_regex(args[pos:]))
    return "".join(out)


def _literal_regex(text: str) -> str:
    out = []
    for c in text:
        if c == ",":
            out.append(r"\s*,\s*")
        elif c.isspace():
            out.append(r"\s+")
        else:
            out.append(re.escape(c))
    return "".join(out)


class CommandPattern:
    """A command template such as ``MEMory:READ? {mcu},{addr},{len}``, compiled for matching.

    ``match_header`` tells whether a line is this command at all; ``match`` also checks the
    parameters and returns them by placeholder name.
    """

    def __init__(self, template: str):
        self.template = template.strip()
        header, _, args = self.template.partition(" ")
        args = args.strip()
        self.is_query = header.endswith("?")
        self.fields = [m.group(1) for m in _PLACEHOLDER.finditer(args)]
        head = r"^\s*" + _header_regex(header)
        self._header = re.compile(head + r"(?:\s|$)", re.IGNORECASE)
        full = head + (r"\s+" + _args_regex(args) if args else "") + r"\s*$"
        self._full = re.compile(full, re.IGNORECASE)

    def match_header(self, line: str) -> bool:
        return self._header.match(line) is not None

    def match(self, line: str) -> dict[str, str] | None:
        m = self._full.match(line)
        return m.groupdict() if m else None


_BARE_HEX = re.compile(r"^[0-9A-Fa-f]+$")


def parse_hex(text: str) -> int:
    """Parse an address or value: bare hex only, such as ``20000000``. ``0x`` prefixes are refused."""
    t = text.strip()
    if not _BARE_HEX.match(t):
        raise ValueError(f"{text!r} is not bare hex")
    return int(t, 16)


def parse_int(text: str, base: int = 0) -> int:
    """Parse an SCPI numeric parameter: decimal, ``0x..``, ``0b..``, ``#H..``, ``#B..`` or ``#Q..``."""
    t = text.strip()
    u = t.upper()
    for prefix, b in (("#H", 16), ("#B", 2), ("#Q", 8)):
        if u.startswith(prefix):
            return int(t[2:], b)
    if base:
        return int(t, base)
    if u.lstrip("+-").startswith(("0X", "0B", "0O")):
        return int(t, 0)
    return int(t, 10)
