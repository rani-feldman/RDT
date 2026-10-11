"""Symbol database: DWARF reading, expressions and typed values, against the test ELF corpus.

The corpus (tests/elf) is one C program built three ways: clang for a little-endian Cortex-M4
with DWARF 4, clang for big-endian ARM with DWARF 5, and GCC for x86-64 (64-bit pointers).
Values are read from the ELF's own initialized data, through the simulator's memory loader.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from rdt import ConfigError, EnumValue, McuConfig, Profile, SymbolError, Target, TargetConfig
from rdt.symbols import Resolver, describe_address, load_symbols, parse
from rdt.symbols.elf import cache_dir
from rdt.symbols.expr import expr_text
from rdt.symbols.types import BaseType, bit_window
from rdt.symbols.values import decode, decode_scalar, encode_scalar, parse_scalar, rows
from sim import Memory

CORPUS = Path(__file__).resolve().parent / "elf"
ELFS = ["corpus-clang.elf", "corpus-clang-be.elf", "corpus-gcc.elf"]


class Image:
    """One corpus ELF: its symbols, and its memory as the firmware starts with it."""

    def __init__(self, name):
        self.path = CORPUS / name
        self.symbols = load_symbols(self.path)
        self.memory = Memory()
        self.order = self.memory.load_elf(self.path)

    def resolve(self, text):
        return Resolver("c", self.symbols, self.order, self.memory.read).resolve(parse(text))

    def value(self, text):
        loc = self.resolve(text)
        data = self.memory.read(loc.addr, loc.size)
        if loc.bit_size is not None:
            return decode_scalar(loc.type, data, self.order, loc.bit_size, loc.bit_shift)
        return decode(loc.type, data, self.order)


@pytest.fixture(params=ELFS, scope="module")
def image(request):
    return Image(request.param)


# --- the symbol table ------------------------------------------------------------------------

def test_header_facts(image):
    expected = {"corpus-clang.elf": ("little", 4), "corpus-clang-be.elf": ("big", 4), "corpus-gcc.elf": ("little", 8)}
    assert (image.symbols.byte_order, image.symbols.pointer_size) == expected[image.path.name]


def test_every_global_and_static_is_found(image):
    names = sorted(v.qualified_name for v in image.symbols.search())
    assert names == sorted([
        "banner", "version", "dev", "nodes", "head", "head_ref", "matrix", "message", "counter", "states",
        "opaque", "on_tick", "zeroed", "symbols.c:shared_name", "other.c:shared_name", "other.c:origin",
    ])


def test_sizes_and_type_names(image):
    p = image.symbols.pointer_size
    want = {
        "dev": ("device_t", 64), "nodes": ("node_t[3]", 6 * p if p == 8 else 24), "head": ("node_t *", p),
        "head_ref": ("node_t **", p), "matrix": ("int16_t[2][3]", 12), "counter": ("volatile uint32_t", 4),
        "states": ("state_t[2]", 8), "zeroed": ("uint8_t[300]", 300), "version": ("ro_reg_t", 4),
        "banner": ("const char[10]", 10), "opaque": ("void *", p), "on_tick": ("handler_t", p),
    }
    for name, (type_name, size) in want.items():
        v = image.symbols.lookup(name)
        assert (v.type.name, v.size) == (type_name, size), name


def test_struct_layout(image):
    dev = image.symbols.find_type("device_t").strip()
    offsets = {m.name: off for off, m in dev.named_members()}
    assert offsets == {"name": 0, "corners": 12, "flags": 20, "state": 24, "raw": 28, "lo": 32, "hi": 34,
                       "gain": 40, "total": 48, "armed": 56}
    assert image.symbols.find_type("point").name == "struct point"   # a tag works as a type name
    assert image.symbols.find_type("state_t").strip().values == {0: "STATE_OFF", 1: "STATE_ON", -1: "STATE_ERROR"}


def test_bit_fields_are_normalized(image):
    flags = image.symbols.find_type("flags_t").strip()
    got = [(m.name, m.offset, m.storage, m.bit_shift, m.bit_size) for m in flags.members if m.name]
    if image.order == "little":
        assert got == [("ready", 0, 4, 0, 1), ("mode", 0, 4, 1, 3), ("level", 0, 4, 4, 5), ("channel", 0, 4, 16, 16)]
    else:
        assert got == [("ready", 0, 4, 31, 1), ("mode", 0, 4, 28, 3), ("level", 0, 4, 23, 5), ("channel", 0, 4, 0, 16)]


def test_file_statics_need_their_file(image):
    with pytest.raises(SymbolError, match="other.c:shared_name, symbols.c:shared_name"):
        image.symbols.lookup("shared_name")
    assert image.value("c:symbols.c:shared_name") == 0x11
    assert image.value("c:other.c:shared_name") == 0x22
    assert image.value("c:origin") == {"x": -1, "y": 1}   # the only one with that name needs no file
    with pytest.raises(SymbolError, match="no variable 'shared_name' in nope.c"):
        image.symbols.lookup("shared_name", "nope.c")


def test_search(image):
    assert [v.name for v in image.symbols.search("HEAD")] == sorted(["head", "head_ref"],
                                                                    key=lambda n: image.symbols.lookup(n).address)
    assert [v.qualified_name for v in image.symbols.search("other.c:*")] == \
        sorted(["other.c:shared_name", "other.c:origin"], key=lambda n: image.symbols.lookup(*n.split(":")[::-1]).address)


# --- values ----------------------------------------------------------------------------------

def test_values_from_initialized_data(image):
    dev = image.value("c:dev")
    assert dev["name"][:5] == b"pump\0"
    assert dev["corners"] == [{"x": 1, "y": -2}, {"x": 300, "y": -400}]
    assert dev["flags"] == {"ready": 1, "mode": 5, "level": -3, "channel": 0xBEEF}
    assert dev["state"] == 1 and dev["state"].name == "STATE_ON"
    assert dev["raw"]["word"] == 0x3FC00000 and dev["raw"]["real"] == 1.5
    assert (dev["lo"], dev["hi"], dev["gain"], dev["total"], dev["armed"]) == (7, 9, 0.25, -5000000000, True)
    assert image.value("c:states[1]") == EnumValue(-1, "STATE_ERROR")
    assert image.value("c:matrix[1][2]") == 6
    assert image.value("c:version") == 0x00010203


def test_pointers_are_followed(image):
    assert image.value("c:head->next->value") == 20
    assert image.value("c:(*head).value") == 10
    assert image.value("c:(*head_ref)->next->next->value") == 30
    assert image.value("c:head[2].value") == 30
    assert image.value("c:*message") == ord("c")
    loc = image.resolve("c:head->next")
    assert loc.dynamic and not image.resolve("c:dev.flags").dynamic
    with pytest.raises(SymbolError, match="next is NULL"):
        image.resolve("c:nodes[2].next->value")
    with pytest.raises(SymbolError, match="can't be followed"):
        image.resolve("c:*opaque")


def test_rows(image):
    loc = image.resolve("c:dev")
    out = rows(loc.type, image.memory.read(loc.addr, loc.size), image.order, loc.path, loc.addr,
               symbolize=lambda a, t: describe_address(image.symbols, a, t))
    shown = {r.path: r.text for r in out}
    assert shown["c:dev"] is None   # a struct's header row
    assert shown["c:dev.name"] == '"pump"'
    assert shown["c:dev.corners[1].y"] == "-400 (0xFE70)"
    assert shown["c:dev.flags.level"] == "-3 (0x1D)"
    assert shown["c:dev.state"] == "STATE_ON (1)"
    assert shown["c:dev.armed"] == "true"
    # uint8_t arrays are bytes, not strings
    assert shown["c:dev.raw.bytes[3]"] == ("63 (0x3F)" if image.order == "little" else "0 (0x00)")
    loc = image.resolve("c:nodes")
    out = rows(loc.type, image.memory.read(loc.addr, loc.size), image.order, loc.path, loc.addr,
               symbolize=lambda a, t: describe_address(image.symbols, a, t))
    shown = {r.path: r.text for r in out}
    assert shown["c:nodes[0].next"].endswith("(&nodes[1])")
    assert shown["c:nodes[2].next"].endswith("(NULL)")


def test_large_arrays_are_cut(image):
    loc = image.resolve("c:zeroed")
    out = rows(loc.type, image.memory.read(loc.addr, loc.size), image.order, loc.path, loc.addr, max_cells=100)
    assert len(out) == 102 and out[-1].text == "... 200 more cells"


def test_describe_address(image):
    s = image.symbols
    nodes = s.lookup("nodes")
    assert describe_address(s, nodes.address) == "&nodes"
    assert describe_address(s, nodes.address, s.find_type("node_t")) == "&nodes[0]"
    assert describe_address(s, nodes.address + nodes.type.strip().element.size + 1) == "&nodes[1].value+1"
    assert describe_address(s, s.lookup("dev").address + 30) == "&dev.raw.word+2"   # a union names its first member
    assert describe_address(s, 0x30000000) is None


# --- writing ---------------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["little", "big"])
def test_bit_field_writes_keep_the_neighbours(order):
    int32 = BaseType("int32_t", 4, "int")
    start, count, shift = bit_window(4, 5, 4, order == "big")
    old = (0xFFFFFFFF).to_bytes(4, order)
    new = encode_scalar(int32, -16, order, count, 5, shift, old)
    assert decode_scalar(int32, new, order, 5, shift) == -16
    word = int.from_bytes(new, order)
    assert word | (0x1F << shift) == 0xFFFFFFFF   # only the field's bits changed


def test_parse_scalar_checks_ranges(image):
    dev = image.symbols.find_type("device_t").strip()
    m = {mm.name: mm for _, mm in dev.named_members()}
    flags = {mm.name: mm for mm in m["flags"].type.strip().members if mm.name}
    assert parse_scalar(flags["level"].type, "-16", 5) == -16
    with pytest.raises(ValueError, match="does not fit in a 5-bit field"):
        parse_scalar(flags["level"].type, "16", 5)
    assert parse_scalar(m["state"].type, "STATE_ERROR") == -1
    assert parse_scalar(m["state"].type, "1") == 1
    with pytest.raises(ValueError, match="use one of STATE_OFF, STATE_ON, STATE_ERROR"):
        parse_scalar(m["state"].type, "STATE_MAYBE")
    assert parse_scalar(m["armed"].type, "false") is False
    assert parse_scalar(m["gain"].type, "2.5e-1") == 0.25
    assert parse_scalar(m["name"].type.strip().element, "'A'") == 65
    with pytest.raises(ValueError, match="does not fit in int16_t"):
        parse_scalar(m["corners"].type.strip().element.strip().members[0].type, "40000")


# --- expressions -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, canonical", [
    ("main:motor.speed", "main:motor.speed"),
    (" main : gains [ 2 ] ", "main:gains[2]"),
    ("main:matrix[1][0x2]", "main:matrix[1][2]"),
    ("main:*head", "main:*head"),
    ("main:(*head).next->value", "main:(*head).next->value"),
    ("main:**head_ref", "main:**head_ref"),
    ("main:motor.c:state", "main:motor.c:state"),
    ("main:src/motor.c:state.x", "main:src/motor.c:state.x"),
    ("@main:0x20000010:u32", "@main:0x20000010:u32"),
    ("@main:536870928:device_t.flags", "@main:0x20000010:device_t.flags"),
    ("@main:0x48000000:node_t*", "@main:0x48000000:node_t*"),
])
def test_parse(text, canonical):
    assert expr_text(parse(text)) == canonical


@pytest.mark.parametrize("text, message", [
    ("motor.speed", "start with the MCU's name"),
    ("main:", "start with the MCU's name"),
    ("main:motor.", "expected more"),
    ("main:gains[x]", "an index must be a number"),
    ("main:gains[1", "expected ]"),
    ("main:a b", "unexpected 'b'"),
    ("main:a+1", "unexpected '\\+1'"),
    ("@main:0x20000010", "expected @mcu:address:type"),
    ("@main:zz:u32", "'zz' is not an address"),
])
def test_parse_errors(text, message):
    with pytest.raises(SymbolError, match=message):
        parse(text)


def test_raw_address_as_an_elf_type(image):
    dev = image.symbols.lookup("dev")
    assert image.value(f"@c:0x{dev.address:X}:device_t.flags.channel") == 0xBEEF
    assert image.value(f"@c:0x{dev.address + 12:X}:point.y") == -2
    with pytest.raises(SymbolError, match="unknown type 'nope_t'"):
        image.resolve("@c:0x20000000:nope_t")


def test_resolve_errors(image):
    for text, message in [
        ("c:nope", "no variable 'nope'"),
        ("c:dev.nope", "device_t has no member 'nope'; it has name, corners"),
        ("c:dev->state", "not a pointer; use '.' instead of '->'"),
        ("c:head.value", "use '->' for a pointer"),
        ("c:matrix[2][0]", "out of range; matrix has 2 cells"),
        ("c:counter[0]", "not an array or a pointer"),
        ("c:dev.flags.mode.x", "is a bit field"),
        ("c:*on_tick", "points to a function"),
    ]:
        with pytest.raises(SymbolError, match=message):
            image.resolve(text)


def test_mcu_without_elf_takes_raw_addresses_only():
    r = Resolver("bare", None, "little")
    assert r.resolve(parse("@bare:0x20000000:u16")).type.size == 2
    with pytest.raises(SymbolError, match="MCU bare has no ELF"):
        r.resolve(parse("bare:motor"))


# --- cache and reload ------------------------------------------------------------------------

def test_symbols_are_cached_by_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("RDT_CACHE_DIR", str(tmp_path))
    first = load_symbols(CORPUS / "corpus-clang.elf")
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1 and cache_dir() == tmp_path
    copy = tmp_path / "copy.elf"
    shutil.copy(CORPUS / "corpus-clang.elf", copy)
    second = load_symbols(copy)   # same bytes, so the same cache entry
    assert len(list(tmp_path.glob("*.pickle"))) == 1
    assert second.path == copy
    assert [(v.qualified_name, v.address, v.type.name) for v in second.search()] == \
           [(v.qualified_name, v.address, v.type.name) for v in first.search()]
    assert second.lookup("head").type.strip().target.strip().member("next")[1].type.strip().target.name == "struct node"
    entries[0].write_bytes(b"garbage")   # a damaged entry is rebuilt
    assert load_symbols(CORPUS / "corpus-clang.elf").lookup("dev").address == first.lookup("dev").address


def test_elf_without_debug_info():
    with pytest.raises(ConfigError, match="has no debug info; build it with -g"):
        load_symbols(CORPUS / "corpus-nodebug.elf", use_cache=False)


def test_reload_remaps_by_name(tmp_path):
    elf = tmp_path / "fw.elf"
    shutil.copy(CORPUS / "corpus-clang.elf", elf)
    cfg = TargetConfig(mcus=[McuConfig(0, "main", elf)], profile=Profile())
    t = Target(cfg)
    dev, counter, opaque = (t.describe(f"main:{n}") for n in ("dev.flags.mode", "counter", "opaque"))
    raw = t.describe("@main:0x20000000:u32")
    assert t.refresh_elfs() == []

    shutil.copy(CORPUS / "corpus-clang-v2.elf", elf)
    assert t.refresh_elfs() == []   # just written: wait for the linker to finish
    old = time.time() - 5
    os.utime(elf, (old, old))
    assert t.refresh_elfs() == ["main"]

    new, note = t.remap(dev)
    assert new.addr == dev.addr + 16 and note == "main:dev.flags.mode moved from 0x20000014 to 0x20000024"
    new, note = t.remap(counter)
    assert new.type.name == "volatile uint16_t"
    assert "is now volatile uint16_t (2 bytes), was volatile uint32_t (4 bytes)" in note
    new, note = t.remap(opaque)
    assert new is None and "main:opaque is gone" in note
    assert t.remap(raw) == (raw, None)
    assert t.describe("main:added_first[3]").addr == 0x2000000C


def test_a_broken_rebuild_keeps_the_old_symbols(tmp_path):
    elf = tmp_path / "fw.elf"
    shutil.copy(CORPUS / "corpus-clang.elf", elf)
    t = Target(TargetConfig(mcus=[McuConfig(0, "main", elf)], profile=Profile()))
    before = t.describe("main:dev").addr
    elf.write_bytes(b"\x7fELF half written")
    old = time.time() - 5
    os.utime(elf, (old, old))
    assert t.refresh_elfs() == []
    assert t.describe("main:dev").addr == before


# --- the Target API over the wire ------------------------------------------------------------

@pytest.fixture(params=["corpus-clang.elf", "corpus-clang-be.elf"])
def corpus_target(request):
    """The simulator with a main MCU and the corpus as MCU 1, behind it in 64-byte frames."""
    from sim import Device, Mcu as SimMcu, Profile as SimProfile, Simulator, TransportConfig
    from host_helpers import FIRMWARE, make_target

    mcus = []
    for mcu_id, path in ((0, FIRMWARE / "main.elf"), (1, CORPUS / request.param)):
        mem = Memory()
        mcus.append(SimMcu(mcu_id, f"m{mcu_id}", mem, mem.load_elf(path)))
    device = Device(mcus, SimProfile(), internal_timeout_ms=5)
    with Simulator(device, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        t = make_target("tcp", s.tcp_address, mcus=[McuConfig(0, "main", FIRMWARE / "main.elf"),
                                                   McuConfig(1, "c", CORPUS / request.param)])
        with t:
            yield t, device


def test_target_reads_and_writes_by_name(corpus_target):
    t, device = corpus_target
    assert t.read_value("c:dev")["flags"] == {"ready": 1, "mode": 5, "level": -3, "channel": 0xBEEF}
    assert t.read_value("c:head->next->value") == 20

    t.write_value("c:dev.flags.level", -16)
    t.write_value("c:dev.flags.mode", "2")
    t.write_value("c:dev.state", "STATE_ERROR")
    t.write_value("c:dev.gain", 0.75)
    t.write_value("c:dev.armed", False)
    dev = t.read_value("c:dev")
    assert dev["flags"] == {"ready": 1, "mode": 2, "level": -16, "channel": 0xBEEF}
    assert (dev["state"].name, dev["gain"], dev["armed"]) == ("STATE_ERROR", 0.75, False)
    with pytest.raises(ConfigError, match="does not fit in a 3-bit field"):
        t.write_value("c:dev.flags.mode", 8)
    with pytest.raises(SymbolError, match="write one field or cell at a time"):
        t.write_value("c:dev.corners", 1)


def test_a_pointer_is_followed_again_on_each_read(corpus_target):
    t, _ = corpus_target
    loc = t.resolve("c:head->value")
    assert t.read_value(loc) == 10
    nodes = t.resolve("c:nodes")
    t.write_value("c:head", nodes.addr + nodes.type.strip().element.size)   # head = &nodes[1]
    assert t.read_value(loc) == 20
    _, shown = t.read_rows("c:head")
    assert shown[0].text.endswith("(&nodes[1])")
