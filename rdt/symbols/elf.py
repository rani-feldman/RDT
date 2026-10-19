"""Symbol table of one MCU, read from its ELF's DWARF debug info.

Every global and file-static variable that has a fixed address is recorded with its type.
Variables the compiler kept only as a constant, locals, and function-static variables are not.
Reading DWARF is slow for a large firmware, so the result is cached on disk, keyed by the hash
of the ELF's bytes: a rebuilt ELF gets a new entry.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from ..errors import ConfigError, SymbolError
from .types import (
    ArrayType, BaseType, EnumType, FunctionType, Member, PointerType, StructType, Type, TypedefType, VOID,
    bit_window,
)

log = logging.getLogger(__name__)

CACHE_VERSION = 2   # bump when the model or the reader changes, so old cache entries are ignored


@dataclass(eq=False)
class Variable:
    name: str
    address: int
    type: Type
    file: str           # the compile unit's source file, as the compiler recorded it
    external: bool      # False for a file-static variable

    @property
    def size(self) -> int:
        return self.type.size

    @property
    def file_name(self) -> str:
        return PurePath(self.file.replace("\\", "/")).name

    @property
    def qualified_name(self) -> str:
        return self.name if self.external else f"{self.file_name}:{self.name}"


@dataclass(eq=False)
class SymbolTable:
    path: Path
    byte_order: str
    pointer_size: int
    variables: list[Variable] = field(default_factory=list)
    types: dict[str, Type] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_name: dict[str, list[Variable]] = {}
        for v in self.variables:
            self._by_name.setdefault(v.name, []).append(v)
        self._sorted = sorted(self.variables, key=lambda v: v.address)

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__post_init__()

    # --- lookups -----------------------------------------------------------------------------

    def lookup(self, name: str, file: str | None = None) -> Variable:
        """Find a variable by name, and by source file for a file-static one.

        A name that only one variable has needs no file. When several have it (file-statics in
        different files, or a global and a static), the global wins; otherwise the file must be
        given, as in ``motor.c:state``.
        """
        found = self._by_name.get(name, [])
        if file is not None:
            found = [v for v in found if _same_file(v.file, file)]
            if not found:
                raise SymbolError(f"no variable {name!r} in {file}")
        if not found:
            raise SymbolError(f"no variable {name!r} in {self.path.name}")
        if len(found) == 1:
            return found[0]
        external = [v for v in found if v.external]
        if len(external) == 1:
            return external[0]
        options = ", ".join(sorted(v.qualified_name for v in found))
        raise SymbolError(f"{name!r} is in more than one file; name the file: {options}")

    def find_type(self, name: str) -> Type | None:
        """A type by its name: a typedef, a base type, or a struct, union or enum tag."""
        name = " ".join(name.split())
        return (self.types.get(name) or self.types.get(f"struct {name}") or self.types.get(f"union {name}")
                or self.types.get(f"enum {name}"))

    def search(self, pattern: str = "*") -> list[Variable]:
        """Variables whose name matches a glob pattern, case-insensitively, by address."""
        if not any(c in pattern for c in "*?["):
            pattern = f"*{pattern}*"
        pat = pattern.lower()
        return [v for v in self._sorted if fnmatch.fnmatchcase(v.name.lower(), pat)
                or fnmatch.fnmatchcase(v.qualified_name.lower(), pat)]

    def at(self, addr: int) -> Variable | None:
        """The variable whose bytes include ``addr``."""
        lo, hi = 0, len(self._sorted)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._sorted[mid].address <= addr:
                lo = mid + 1
            else:
                hi = mid
        for v in reversed(self._sorted[max(0, lo - 4):lo]):
            if v.address <= addr < v.address + max(v.size, 1):
                return v
        return None


def _same_file(recorded: str, given: str) -> bool:
    rec = recorded.replace("\\", "/").lower()
    giv = given.replace("\\", "/").lower()
    return rec == giv or rec.endswith("/" + giv)


# --- loading ---------------------------------------------------------------------------------

def cache_dir() -> Path:
    if os.environ.get("RDT_CACHE_DIR"):
        return Path(os.environ["RDT_CACHE_DIR"])
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "rdt" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "rdt"


def load_symbols(path: str | Path, use_cache: bool = True) -> SymbolTable:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"cannot read ELF {path}: {e.strerror}") from None
    key = hashlib.sha256(data).hexdigest()[:32]
    cached = cache_dir() / f"symbols-v{CACHE_VERSION}-{key}.pickle"
    if use_cache:
        try:
            with open(cached, "rb") as f:
                table = pickle.load(f)
            table.path = path
            return table
        except FileNotFoundError:
            pass
        except Exception as e:   # a damaged or foreign cache entry is rebuilt
            log.info("ignoring symbol cache %s: %s", cached, e)
    table = read_dwarf(path)
    if use_cache:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "wb") as f:
                pickle.dump(table, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cached)
        except (OSError, RecursionError) as e:
            log.info("cannot write symbol cache %s: %s", cached, e)
    return table


def read_dwarf(path: Path) -> SymbolTable:
    from elftools.common.exceptions import DWARFError, ELFError
    from elftools.elf.elffile import ELFFile

    try:
        with open(path, "rb") as f:
            elf = ELFFile(f)
            order = "little" if elf.little_endian else "big"
            ptr = 8 if elf.elfclass == 64 else 4
            if not elf.has_dwarf_info():
                raise ConfigError(f"{path.name} has no debug info; build it with -g to use variable names")
            reader = _DwarfReader(elf.get_dwarf_info(), order == "big", ptr)
            reader.read()
    except OSError as e:
        raise ConfigError(f"cannot read ELF {path}: {e.strerror}") from None
    except (ELFError, DWARFError) as e:
        raise ConfigError(f"cannot read the debug info in {path.name}: {e}") from None
    except RecursionError:   # types nested too deeply even without following pointers
        raise ConfigError(f"cannot read the debug info in {path.name}: its types are nested too deeply") from None
    return SymbolTable(path, order, ptr, reader.variables, reader.named_types)


_ENCODINGS = {
    0x01: "uint",    # DW_ATE_address
    0x02: "bool",
    0x04: "float",
    0x05: "int",
    0x06: "char",    # DW_ATE_signed_char
    0x07: "uint",
    0x08: "uchar",   # DW_ATE_unsigned_char
    0x10: "uint",    # DW_ATE_UTF
}

_QUALIFIERS = {
    "DW_TAG_const_type": "const", "DW_TAG_volatile_type": "volatile",
    "DW_TAG_restrict_type": "restrict", "DW_TAG_atomic_type": "_Atomic",
}


class _DwarfReader:
    def __init__(self, dwarf, big_endian: bool, pointer_size: int):
        from elftools.dwarf.dwarf_expr import DWARFExprParser

        self.dwarf = dwarf
        self.big = big_endian
        self.ptr = pointer_size
        self.expr = DWARFExprParser(dwarf.structs)
        self.types: dict[int, Type] = {}
        self.named_types: dict[str, Type] = {}
        self.variables: list[Variable] = []
        # Pointers whose pointee is still to be read, and types whose name or size comes from
        # parts that may not be finished when they are made (see _finish).
        self._pointees: list[tuple[PointerType, object]] = []
        self._derived: list[tuple] = []

    def read(self) -> None:
        for cu in self.dwarf.iter_CUs():
            top = cu.get_top_DIE()
            if top.tag != "DW_TAG_compile_unit":
                continue
            file = _str(top, "DW_AT_name") or "?"
            for die in top.iter_children():
                if die.tag == "DW_TAG_variable":
                    self._variable(cu, die, file)
                elif die.tag in ("DW_TAG_typedef", "DW_TAG_structure_type", "DW_TAG_union_type",
                                 "DW_TAG_enumeration_type", "DW_TAG_base_type"):
                    self._type(die)
        self._finish()

    def _finish(self) -> None:
        """Read the pointees, then fill in names and sizes made from unfinished parts.

        A pointer doesn't read its pointee straight away: a long chain of structs linked by
        pointers would otherwise recurse once per link and overflow the stack (review L3). And a
        typedef that is still being read, because its struct points back to the struct that uses
        it, has size 0 until it is finished, so an array of it or a ``const`` of it made in the
        meantime would keep size 0 (review M9). So these are patched here, once everything is read.
        """
        while self._pointees:
            t, die = self._pointees.pop()
            t.target = self._type(die)
        # A derived type's parts can be derived types themselves; repeat until nothing changes.
        # This ends: C types only loop through a struct, union or enum tag, whose names are fixed.
        changed = True
        while changed:
            changed = False
            for kind, t, *extra in self._derived:
                if kind == "pointer":
                    name, size = _pointer_name(t.target), t.size
                elif kind == "typedef":
                    name, size = t.name, t.strip().size
                elif kind == "qualifier":
                    q = extra[0]
                    name = t.target.name if t.target.name.split()[0] == q else f"{q} {t.target.name}"
                    size = t.strip().size
                else:   # array: its own element, and the [n] suffix it adds to the element's name
                    base, suffix = extra
                    name, size = base.name + suffix, (t.count or 0) * t.element.size
                if (name, size) != (t.name, t.size):
                    t.name, t.size, changed = name, size, True

    # --- variables ---------------------------------------------------------------------------

    def _variable(self, cu, die, file: str) -> None:
        if "DW_AT_location" not in die.attributes:
            return   # a declaration, or kept only as a constant
        addr = self._address(cu, die.attributes["DW_AT_location"])
        if addr is None:
            return
        decl = die.get_DIE_from_attribute("DW_AT_specification") if "DW_AT_specification" in die.attributes else die
        name = _str(die, "DW_AT_name") or _str(decl, "DW_AT_name")
        type_die = die if "DW_AT_type" in die.attributes else decl
        if not name or "DW_AT_type" not in type_die.attributes:
            return
        external = bool(_attr(die, "DW_AT_external") or _attr(decl, "DW_AT_external"))
        self.variables.append(Variable(name, addr, self._ref(type_die), file, external))

    def _address(self, cu, attr) -> int | None:
        """The address of a variable with a fixed location (``DW_OP_addr``), else None."""
        if attr.form in ("DW_FORM_sec_offset", "DW_FORM_loclistx") or isinstance(attr.value, int):
            return None   # a location list: not a static variable
        try:
            ops = self.expr.parse_expr(attr.value)
        except Exception:
            return None
        if not ops:
            return None
        first = ops[0]
        if first.op_name == "DW_OP_addr":
            addr = first.args[0]
        elif first.op_name in ("DW_OP_addrx", "DW_OP_GNU_addr_index"):
            addr = self.dwarf.get_addr(cu, first.args[0])
        else:
            return None
        for op in ops[1:]:
            if op.op_name == "DW_OP_plus_uconst":
                addr += op.args[0]
            else:
                return None   # thread-local or computed: not a plain address
        return addr

    # --- types -------------------------------------------------------------------------------

    def _ref(self, die) -> Type:
        if "DW_AT_type" not in die.attributes:
            return VOID
        return self._type(die.get_DIE_from_attribute("DW_AT_type"))

    def _type(self, die) -> Type:
        if die.offset in self.types:
            return self.types[die.offset]
        tag = die.tag
        name = _str(die, "DW_AT_name")
        size = _attr(die, "DW_AT_byte_size")

        if tag == "DW_TAG_base_type":
            enc = _ENCODINGS.get(_attr(die, "DW_AT_encoding"), "uint")
            t: Type = BaseType(name or f"<{size}-byte>", size or 0, enc)
            self._keep(die, t, name)
        elif tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
            union = tag == "DW_TAG_union_type"
            kw = "union" if union else "struct"
            t = StructType(f"{kw} {name}" if name else f"{kw} {{...}}", size or 0, union)
            self._keep(die, t, t.name if name else None)   # before the members: they may point back
            if not _attr(die, "DW_AT_declaration"):
                t.members = [self._member(m) for m in die.iter_children() if m.tag == "DW_TAG_member"]
        elif tag == "DW_TAG_enumeration_type":
            base = self._ref(die).strip() if "DW_AT_type" in die.attributes else None
            signed = isinstance(base, BaseType) and base.signed
            values: dict[int, str] = {}
            for e in die.iter_children():
                if e.tag != "DW_TAG_enumerator":
                    continue
                attr = e.attributes["DW_AT_const_value"]
                v = attr.value
                if attr.form == "DW_FORM_sdata" and v < 0:
                    signed = True
                values[v] = _str(e, "DW_AT_name")
            if signed and size:
                values = {_signed(v, size): n for v, n in values.items()}
            t = EnumType(f"enum {name}" if name else "enum {...}", size or 4, signed, values)
            self._keep(die, t, t.name if name else None)
        elif tag == "DW_TAG_pointer_type" or tag == "DW_TAG_reference_type":
            t = PointerType("?", size or self.ptr)
            self.types[die.offset] = t
            if "DW_AT_type" in die.attributes:   # the pointee and the name come in _finish
                self._pointees.append((t, die.get_DIE_from_attribute("DW_AT_type")))
                self._derived.append(("pointer", t))
            else:
                t.name = _pointer_name(None)
        elif tag == "DW_TAG_typedef":
            t = TypedefType(name or "?", 0)
            self._keep(die, t, name)
            t.target = self._ref(die)
            t.size = t.strip().size
            self._derived.append(("typedef", t))
        elif tag in _QUALIFIERS:
            t = TypedefType("?", 0)
            self.types[die.offset] = t
            t.target = self._ref(die)
            q = _QUALIFIERS[tag]
            t.name = t.target.name if t.target.name.split()[0] == q else f"{q} {t.target.name}"
            t.size = t.strip().size
            self._derived.append(("qualifier", t, q))
        elif tag == "DW_TAG_array_type":
            t = self._array(die)
        elif tag in ("DW_TAG_subroutine_type", "DW_TAG_subprogram"):
            t = FunctionType("function", 0)
            self.types[die.offset] = t
        else:
            t = Type(name or tag.removeprefix("DW_TAG_"), size or 0)
            self.types[die.offset] = t
        return t

    def _keep(self, die, t: Type, name: str | None) -> None:
        self.types[die.offset] = t
        if name and name not in self.named_types:
            self.named_types[name] = t

    def _array(self, die) -> Type:
        element = self._ref(die)
        dims: list[int | None] = []
        for sub in die.iter_children():
            if sub.tag != "DW_TAG_subrange_type":
                continue
            count = _const(sub, "DW_AT_count")
            upper = _const(sub, "DW_AT_upper_bound")
            lower = _const(sub, "DW_AT_lower_bound") or 0
            if count is not None:
                dims.append(count)
            elif upper is not None and lower <= upper < 0xFFFFFFFF:
                dims.append(upper - lower + 1)
            else:
                dims.append(None)   # unknown size, such as a flexible array member
        if not dims:
            dims = [None]
        t: Type = element
        suffix = ""
        for n in reversed(dims):
            suffix = f"[{'' if n is None else n}]" + suffix
            t = ArrayType(element.name + suffix, (n or 0) * t.size, t, n)
            self._derived.append(("array", t, element, suffix))
        self.types[die.offset] = t
        return t

    def _member(self, die) -> Member:
        mtype = self._ref(die)
        name = _str(die, "DW_AT_name")
        loc = die.attributes.get("DW_AT_data_member_location")
        offset = 0
        if loc is not None:
            if isinstance(loc.value, int):
                offset = loc.value
            else:
                ops = self.expr.parse_expr(loc.value)
                if ops and ops[0].op_name == "DW_OP_plus_uconst":
                    offset = ops[0].args[0]
        bits = _attr(die, "DW_AT_bit_size")
        if bits is None:
            return Member(name, offset, mtype)
        unit = mtype.strip().size or 4
        if "DW_AT_data_bit_offset" in die.attributes:
            bit_offset = die.attributes["DW_AT_data_bit_offset"].value
        else:
            # DWARF 2 and 3: DW_AT_bit_offset counts from the most significant bit of a storage
            # unit of DW_AT_byte_size bytes at the member's location.
            unit = _attr(die, "DW_AT_byte_size") or unit
            from_msb = _bit_offset(die.attributes.get("DW_AT_bit_offset"), unit)
            bit_offset = 8 * offset + (from_msb if self.big else 8 * unit - from_msb - bits)
        start, count, shift = bit_window(bit_offset, bits, unit, self.big)
        return Member(name, start, mtype, bits, shift, count)


def _attr(die, name):
    a = die.attributes.get(name)
    return a.value if a is not None else None


def _bit_offset(attr, unit: int) -> int:
    """DWARF 2/3 ``DW_AT_bit_offset``, which is negative for a bit field that crosses its storage
    unit (packed structs). Compilers emit it in an unsigned ``DW_FORM_data<n>`` form, as the
    two's complement of the negative value, so a value too big to be a bit position in the
    unit is converted back (review L2)."""
    if attr is None:
        return 0
    v = attr.value
    width = {"DW_FORM_data1": 1, "DW_FORM_data2": 2, "DW_FORM_data4": 4, "DW_FORM_data8": 8}.get(attr.form)
    if width and v >= 8 * unit:
        v = _signed(v, width)
    return v


def _const(die, name) -> int | None:
    """An integer attribute, or None if it is missing or not a constant (a variable-length bound)."""
    a = die.attributes.get(name)
    if a is None or a.form.startswith("DW_FORM_ref") or not isinstance(a.value, int):
        return None
    return a.value


def _str(die, name) -> str | None:
    v = _attr(die, name)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _signed(v: int, size: int) -> int:
    bits = 8 * size
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def _pointer_name(target: Type | None) -> str:
    if target is None:
        return "void *"
    if isinstance(target, FunctionType):
        return "function *"
    return target.name + (" *" if not target.name.endswith("*") else "*")
