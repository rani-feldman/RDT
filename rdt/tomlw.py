"""A small TOML writer for the files RDT saves: tables, arrays of tables, and plain values.

Python reads TOML (``tomllib``) but can't write it, and the workspace needs no more than this.
"""

from __future__ import annotations

import math
import re


class Hex(int):
    """An integer written in hex, such as an address."""


_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def dumps(data: dict, header: str = "") -> str:
    out: list[str] = [f"# {line}".rstrip() for line in header.splitlines()]
    _table(out, data, [])
    return "\n".join(out).strip("\n") + "\n"


def _key(k: str) -> str:
    return k if _BARE.match(k) else _str(k)


def _is_table_array(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _table(out: list[str], data: dict, path: list[str]) -> None:
    plain = [(k, v) for k, v in data.items() if v is not None and not isinstance(v, dict) and not _is_table_array(v)]
    width = max((len(_key(k)) for k, _ in plain), default=0)
    for k, v in plain:
        out.append(f"{_key(k):<{width}} = {_value(v)}")
    for k, v in data.items():
        if isinstance(v, dict):
            out.append("")
            out.append(f"[{'.'.join(_key(p) for p in path + [k])}]")
            _table(out, v, path + [k])
        elif _is_table_array(v):
            for item in v:
                out.append("")
                out.append(f"[[{'.'.join(_key(p) for p in path + [k])}]]")
                _table(out, item, path + [k])


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Hex):
        return f"0x{int(v):X}" if v >= 0 else str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        text = repr(v)
        return text if any(c in text for c in ".en") else text + ".0"
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _str(s: str) -> str:
    out = ['"']
    for c in s:
        if c in '"\\':
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\r":
            out.append("\\r")
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)
