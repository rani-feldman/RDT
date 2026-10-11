"""Type model built from DWARF: what a variable's bytes mean.

Typedefs, ``const`` and ``volatile`` are kept only in a type's display name; ``strip()`` gives
the type underneath. Bit fields are not types of their own: a ``Member`` carries the bit
position, normalized so that every reader of the model can ignore the DWARF version and the
target's byte order (see ``Member``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False)
class Type:
    name: str
    size: int

    def strip(self) -> Type:
        return self

    def __str__(self) -> str:
        return self.name


@dataclass(eq=False)
class BaseType(Type):
    kind: str = "uint"   # int | uint | float | bool | char | uchar

    @property
    def signed(self) -> bool:
        return self.kind in ("int", "char")


@dataclass(eq=False)
class EnumType(Type):
    signed: bool = False
    values: dict[int, str] = field(default_factory=dict)

    def value_of(self, name: str) -> int | None:
        for v, n in self.values.items():
            if n == name:
                return v
        return None


@dataclass(eq=False)
class PointerType(Type):
    target: Type | None = None   # None is void


@dataclass(eq=False)
class FunctionType(Type):
    pass


@dataclass(eq=False)
class ArrayType(Type):
    element: Type | None = None
    count: int | None = None     # None for an array of unknown size, such as ``x[]``


@dataclass(eq=False)
class Member:
    """A struct or union member.

    For a bit field, ``offset`` and ``storage`` give the bytes that hold it, and the field is
    ``bit_size`` bits starting ``bit_shift`` bits above the least significant bit of those bytes
    read as one integer in the target's byte order.
    """

    name: str | None             # None for an anonymous struct or union member
    offset: int
    type: Type
    bit_size: int | None = None
    bit_shift: int = 0
    storage: int = 0

    @property
    def is_bitfield(self) -> bool:
        return self.bit_size is not None


@dataclass(eq=False)
class StructType(Type):
    union: bool = False
    members: list[Member] = field(default_factory=list)

    def member(self, name: str) -> tuple[int, Member] | None:
        """Find a member by name, looking inside anonymous members too. Returns (offset, member)."""
        for m in self.members:
            if m.name == name:
                return m.offset, m
            if m.name is None:
                inner = m.type.strip()
                if isinstance(inner, StructType):
                    found = inner.member(name)
                    if found:
                        return m.offset + found[0], found[1]
        return None

    def named_members(self) -> list[tuple[int, Member]]:
        """Members by name, with anonymous members' fields lifted into this struct."""
        out = []
        for m in self.members:
            inner = m.type.strip()
            if m.name is None and isinstance(inner, StructType):
                out += [(m.offset + off, sub) for off, sub in inner.named_members()]
            elif m.name is not None:
                out.append((m.offset, m))
        return out


@dataclass(eq=False)
class TypedefType(Type):
    target: Type | None = None

    def strip(self) -> Type:
        t: Type | None = self.target
        seen = 0
        while isinstance(t, TypedefType) and seen < 64:
            t, seen = t.target, seen + 1
        return t if t is not None else VOID


VOID = Type("void", 0)


def is_scalar(t: Type) -> bool:
    return isinstance(t.strip(), (BaseType, EnumType, PointerType))


def is_char(t: Type) -> bool:
    """A ``char`` as written in the source; ``uint8_t`` and ``int8_t`` are numbers, not characters."""
    words = [w for w in t.name.split() if w not in ("const", "volatile")]
    return words in (["char"], ["signed", "char"], ["unsigned", "char"]) and t.size == 1


def is_char_array(t: Type) -> bool:
    """An array of ``char``, shown as a string. ``uint8_t`` buffers stay byte arrays."""
    t = t.strip()
    return isinstance(t, ArrayType) and t.element is not None and is_char(t.element)


def bit_window(bit_offset: int, bit_size: int, unit: int, big_endian: bool) -> tuple[int, int, int]:
    """Bytes that hold a bit field, and its shift within them.

    ``bit_offset`` counts from the start of the struct, in the target's bit numbering (from the
    least significant bit of byte 0 on a little-endian target, from the most significant bit of
    byte 0 on a big-endian one), as DWARF's ``DW_AT_data_bit_offset`` does. ``unit`` is the byte
    size of the field's declared type. Returns (byte offset, byte count, shift).
    """
    first, last = bit_offset // 8, (bit_offset + bit_size - 1) // 8
    start, count = first, last - first + 1
    if unit in (1, 2, 4, 8) and (first // unit) == (last // unit):
        start, count = first - first % unit, unit   # the declared storage unit, aligned
    rel = bit_offset - 8 * start
    shift = 8 * count - rel - bit_size if big_endian else rel
    return start, count, shift
