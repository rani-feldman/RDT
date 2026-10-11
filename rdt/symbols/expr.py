"""Expressions that name a value on one MCU, and resolving them to an address and a type.

    main:motor.speed            a global, then a member
    main:gains[2]               an array cell; arrays of arrays as matrix[1][2]
    main:*head  main:head->next->value  main:(*head).value
    main:motor.c:state          a file-static variable, named with its source file
    @main:0x20000418:u32        a raw address as one of the scalar types
    @main:0x48000000:GPIO_TypeDef.ODR   a raw address as any type from the ELF

Every expression starts with its MCU's name. Following a pointer reads it from the device, so
an expression that goes through one is resolved again before each read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .. import codec
from ..errors import SymbolError
from ..profile import parse_int
from .elf import SymbolTable
from .types import ArrayType, BaseType, FunctionType, PointerType, StructType, Type

# --- syntax tree -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Var:
    name: str
    file: str | None = None


@dataclass(frozen=True)
class Raw:
    addr: int
    type: str


@dataclass(frozen=True)
class Member:
    base: object
    name: str
    arrow: bool = False


@dataclass(frozen=True)
class Index:
    base: object
    index: int


@dataclass(frozen=True)
class Deref:
    base: object


@dataclass(frozen=True)
class Expr:
    mcu: str
    node: object


def text_of(node) -> str:
    if isinstance(node, Var):
        return f"{node.file}:{node.name}" if node.file else node.name
    if isinstance(node, Raw):
        return f"0x{node.addr:08X}:{node.type}"
    if isinstance(node, Deref):
        return "*" + text_of(node.base)
    base = text_of(node.base)
    if isinstance(node.base, Deref):
        base = f"({base})"
    if isinstance(node, Member):
        return f"{base}{'->' if node.arrow else '.'}{node.name}"
    return f"{base}[{node.index}]"


def expr_text(e: Expr) -> str:
    prefix = "@" if _root(e.node).__class__ is Raw else ""
    return f"{prefix}{e.mcu}:{text_of(e.node)}"


def _root(node):
    while not isinstance(node, (Var, Raw)):
        node = node.base
    return node


# --- parser ----------------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(->|[A-Za-z_]\w*|0[xX][0-9A-Fa-f]+|\d+|[.\[\]()*])")
_TYPE_NAME = re.compile(r"[A-Za-z_]\w*(\s*\*)*")
_EXAMPLES = "such as main:motor.speed or @main:0x20000010:u32"


def parse(text: str) -> Expr:
    t = text.strip()
    if t.startswith("@"):
        parts = t[1:].split(":", 2)
        if len(parts) != 3 or not parts[0]:
            raise SymbolError(f"{text!r}: expected @mcu:address:type, such as @main:0x20000010:u32")
        mcu, addr, rest = parts
        try:
            address = parse_int(addr)
        except ValueError:
            raise SymbolError(f"{text!r}: {addr!r} is not an address") from None
        m = _TYPE_NAME.match(rest.strip())
        if not m:
            raise SymbolError(f"{text!r}: expected a type after the address, such as u32 or a type from the ELF")
        node = Raw(address, " ".join(m.group(0).replace("*", " * ").split()).replace(" *", "*"))
        node = _Parser(text, rest.strip()[m.end():]).postfix_only(node)
        return Expr(mcu, node)
    mcu, sep, rest = t.partition(":")
    if not sep or not mcu or not rest:
        raise SymbolError(f"{text!r}: start with the MCU's name, {_EXAMPLES}")
    file = None
    if ":" in rest:
        file, _, rest = rest.rpartition(":")
        file = file.strip()
    return Expr(mcu.strip(), _Parser(text, rest, file).parse())


class _Parser:
    def __init__(self, full: str, text: str, file: str | None = None):
        self.full = full
        self.file = file
        self.tokens: list[str] = []
        pos = 0
        text = text.rstrip()
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m:
                raise SymbolError(f"{full!r}: unexpected {text[pos:].strip()[:10]!r}")
            self.tokens.append(m.group(1))
            pos = m.end()
        self.i = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, want: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (want is not None and tok != want):
            found = f"{tok!r}" if tok else "the end"
            raise SymbolError(f"{self.full!r}: expected {want or 'more'} but found {found}")
        self.i += 1
        return tok

    def parse(self):
        node = self.unary()
        if self.peek() is not None:
            raise SymbolError(f"{self.full!r}: unexpected {self.peek()!r}")
        return node

    def postfix_only(self, node):
        node = self.postfix(node)
        if self.peek() is not None:
            raise SymbolError(f"{self.full!r}: unexpected {self.peek()!r}")
        return node

    def unary(self):
        if self.peek() == "*":
            self.take()
            return Deref(self.unary())
        return self.postfix(self.primary())

    def primary(self):
        tok = self.take()
        if tok == "(":
            node = self.unary()
            self.take(")")
            return node
        if not re.match(r"[A-Za-z_]", tok):
            raise SymbolError(f"{self.full!r}: expected a variable name but found {tok!r}")
        node = Var(tok, self.file)
        self.file = None
        return node

    def postfix(self, node):
        while (tok := self.peek()) in (".", "->", "["):
            self.take()
            if tok == "[":
                idx = self.take()
                try:
                    n = parse_int(idx)
                except ValueError:
                    raise SymbolError(f"{self.full!r}: an index must be a number, not {idx!r}") from None
                self.take("]")
                node = Index(node, n)
            else:
                name = self.take()
                if not re.match(r"[A-Za-z_]\w*$", name):
                    raise SymbolError(f"{self.full!r}: expected a member name after {tok!r}")
                node = Member(node, name, tok == "->")
        return node


# --- resolving -------------------------------------------------------------------------------

# The raw types of M2 as base types, so a raw address and a variable are read the same way.
RAW_TYPES = {
    name: BaseType(name, codec.size_of(name), "float" if name[0] == "f" else ("int" if name[0] == "i" else "uint"))
    for name in codec.TYPES
}


@dataclass(frozen=True)
class Location:
    """Where a value is on one MCU, and what it is."""

    mcu: str
    path: str               # the expression, written the canonical way
    addr: int
    type: Type
    bit_size: int | None = None
    bit_shift: int = 0
    storage: int = 0        # for a bit field: how many bytes hold it
    dynamic: bool = False   # went through a pointer: resolve again before each read

    @property
    def size(self) -> int:
        return self.storage if self.bit_size is not None else self.type.size

    @property
    def type_name(self) -> str:
        return f"{self.type.name} : {self.bit_size}" if self.bit_size is not None else self.type.name


Reader = Callable[[int, int], bytes]


class Resolver:
    def __init__(self, mcu: str, symbols: SymbolTable | Callable[[], SymbolTable | None] | None, byte_order: str,
                 reader: Reader | None = None):
        """``symbols`` may be a function that loads them, so a raw address never waits for DWARF."""
        self.mcu = mcu
        self._symbols = symbols
        self.order = byte_order
        self.reader = reader

    @property
    def symbols(self) -> SymbolTable | None:
        if callable(self._symbols):
            self._symbols = self._symbols()
        return self._symbols

    @property
    def pointer_size(self) -> int:
        return self.symbols.pointer_size if self.symbols else 4

    def resolve(self, e: Expr) -> Location:
        loc = self._node(e.node)
        return Location(self.mcu, expr_text(e), loc.addr, loc.type, loc.bit_size, loc.bit_shift, loc.storage,
                        loc.dynamic)

    def _need_symbols(self, what: str) -> SymbolTable:
        if self.symbols is None:
            raise SymbolError(f"MCU {self.mcu} has no ELF, so {what} can't be resolved; "
                              f"use a raw address such as @{self.mcu}:0x20000010:u32")
        return self.symbols

    def find_type(self, name: str) -> Type:
        if name.endswith("*"):
            target = self.find_type(name[:-1].strip())
            return PointerType(target.name + ("*" if target.name.endswith("*") else " *"), self.pointer_size, target)
        if name in RAW_TYPES:
            return RAW_TYPES[name]
        t = self.symbols.find_type(name) if self.symbols else None
        if t is None:
            hint = "" if self.symbols else f" (MCU {self.mcu} has no ELF)"
            raise SymbolError(f"unknown type {name!r}{hint}; use one of {', '.join(codec.TYPES)} "
                              f"or a type from the ELF")
        return t

    def _node(self, node) -> Location:
        if isinstance(node, Var):
            v = self._need_symbols(repr(node.name)).lookup(node.name, node.file)
            return Location(self.mcu, "", v.address, v.type)
        if isinstance(node, Raw):
            return Location(self.mcu, "", node.addr, self.find_type(node.type))
        base = self._node(node.base)
        what = text_of(node.base)
        if base.bit_size is not None:
            raise SymbolError(f"{what} is a bit field")
        t = base.type.strip()
        if isinstance(node, Member):
            if node.arrow:
                if not isinstance(t, PointerType):
                    raise SymbolError(f"{what} is a {base.type.name}, not a pointer; use '.' instead of '->'")
                base = self._deref(base, what)
                t = base.type.strip()
            if not isinstance(t, StructType):
                hint = "; use '->' for a pointer" if isinstance(t, PointerType) and not node.arrow else ""
                raise SymbolError(f"{what} is a {base.type.name}, not a struct or union{hint}")
            found = t.member(node.name)
            if found is None:
                names = ", ".join(m.name for _, m in t.named_members())
                raise SymbolError(f"{base.type.name} has no member {node.name!r}; it has {names}")
            off, m = found
            if m.is_bitfield:
                return Location(self.mcu, "", base.addr + off, m.type, m.bit_size, m.bit_shift, m.storage,
                                base.dynamic)
            return Location(self.mcu, "", base.addr + off, m.type, dynamic=base.dynamic)
        if isinstance(node, Index):
            if isinstance(t, ArrayType):
                if t.count is not None and not 0 <= node.index < t.count:
                    raise SymbolError(f"{what}[{node.index}] is out of range; {what} has {t.count} cells "
                                      f"(0..{t.count - 1})")
                return Location(self.mcu, "", base.addr + node.index * t.element.size, t.element,
                                dynamic=base.dynamic)
            if isinstance(t, PointerType):
                target = self._deref(base, what)
                return Location(self.mcu, "", target.addr + node.index * target.type.size, target.type,
                                dynamic=True)
            raise SymbolError(f"{what} is a {base.type.name}, not an array or a pointer")
        if isinstance(node, Deref):
            if isinstance(t, ArrayType):
                return Location(self.mcu, "", base.addr, t.element, dynamic=base.dynamic)
            if not isinstance(t, PointerType):
                raise SymbolError(f"{what} is a {base.type.name}, not a pointer")
            return self._deref(base, what)
        raise AssertionError(node)

    def _deref(self, ptr: Location, what: str) -> Location:
        t = ptr.type.strip()
        assert isinstance(t, PointerType)
        if t.target is not None and isinstance(t.target.strip(), FunctionType):
            raise SymbolError(f"{what} points to a function")
        if t.target is None or t.target.strip().size == 0 and not isinstance(t.target.strip(), StructType):
            raise SymbolError(f"{what} is a {ptr.type.name}; it can't be followed")
        if self.reader is None:
            raise SymbolError(f"following {what} needs the device")
        value = int.from_bytes(self.reader(ptr.addr, t.size), self.order)
        if value == 0:
            raise SymbolError(f"{what} is NULL")
        return Location(self.mcu, "", value, t.target, dynamic=True)


def describe_address(symbols: SymbolTable, addr: int, target: Type | None = None) -> str | None:
    """``&name``, ``&arr[3].field`` or ``&name+5`` for an address inside a known variable.

    With the pointer's ``target`` type, it goes down to the part of that type, so a ``node_t *``
    to the start of ``node_t nodes[3]`` reads ``&nodes[0]`` rather than ``&nodes``.
    """
    v = symbols.at(addr)
    if v is None:
        return None
    want = target.strip().name if target is not None else None
    path, off, t = v.qualified_name, addr - v.address, v.type.strip()
    while off > 0 or (want is not None and t.name != want and isinstance(t, (ArrayType, StructType))):
        if isinstance(t, ArrayType) and t.element.size:
            i = off // t.element.size
            path, off, t = f"{path}[{i}]", off - i * t.element.size, t.element.strip()
        elif isinstance(t, StructType):
            for m_off, m in t.named_members():
                size = m.storage if m.is_bitfield else m.type.size
                if not m.is_bitfield and m_off <= off < m_off + size:
                    path, off, t = f"{path}.{m.name}", off - m_off, m.type.strip()
                    break
            else:
                break
        else:
            break
    return f"&{path}" + (f"+{off}" if off else "")
