import pytest

from rdt import AccessRefused, codec
from rdt.memmap import Area, MemoryMap, load_elf_sections
from rdt.profile import Range, align_span
from rdt.session import split_write

from host_helpers import FIRMWARE


@pytest.fixture
def mm():
    elf = load_elf_sections(FIRMWARE / "main.elf")
    return MemoryMap(elf.sections, [Range("GPIOA", 0x48000000, 0x400), Range("ROM", 0x1FFF0000, 16, writable=False)])


def test_elf_sections():
    elf = load_elf_sections(FIRMWARE / "main.elf")
    assert elf.byte_order == "little"
    names = {a.name: (a.start, a.size, a.writable) for a in elf.sections}
    assert names[".data"] == (0x20000000, 36, True)
    assert names[".bss"] == (0x20000024, 200, True)
    assert names[".text"][2] is False


def test_reads_inside_known_memory(mm):
    mm.check(0x20000000, 4)
    mm.check(0x20000020, 16)             # .data into .bss
    mm.check(0x08000000, 16)             # flash can be read
    mm.check(0x48000000, 0x400)


@pytest.mark.parametrize("addr,size", [
    (0x30000000, 4),                     # nothing there
    (0x200000EA, 4),                     # runs off the end of .bss
    (0x47FFFFFE, 4),                     # into the start of GPIOA from outside
])
def test_refused_outside_known_memory(mm, addr, size):
    with pytest.raises(AccessRefused):
        mm.check(addr, size)


def test_listed_ranges_are_never_merged_with_neighbours():
    mm = MemoryMap(ranges=[Range("A", 0x40000000, 16), Range("B", 0x40000010, 16)])
    mm.check(0x40000000, 16)
    with pytest.raises(AccessRefused, match="crosses"):
        mm.check(0x40000008, 16)


def test_writes_to_read_only_memory_are_refused(mm):
    mm.check(0x20000000, 4, write=True)
    for addr in (0x08000000, 0x1FFF0000):
        with pytest.raises(AccessRefused, match="read-only"):
            mm.check(addr, 4, write=True)


def test_split_write_is_aligned_and_no_wider_than_max_write():
    assert split_write(0x100, bytes(8), 4) == [(0x100, bytes(4)), (0x104, bytes(4))]
    assert [(a, len(d)) for a, d in split_write(0x101, bytes(7), 4)] == [(0x101, 1), (0x102, 2), (0x104, 4)]
    assert [len(d) for _, d in split_write(0x100, bytes(4), 1)] == [1, 1, 1, 1]


@pytest.mark.parametrize("t,value", [("u8", 255), ("i8", -128), ("u16", 65535), ("i32", -5), ("u64", 2**64 - 1),
                                     ("f32", 1.5), ("f64", -0.1)])
def test_codec_round_trip(t, value):
    for order in ("little", "big"):
        assert codec.decode(codec.encode(value, t, order), t, order) == value


def test_codec_checks_range_and_parses_user_values():
    with pytest.raises(ValueError):
        codec.encode(256, "u8", "little")
    with pytest.raises(ValueError):
        codec.encode(-1, "u16", "little")
    assert codec.parse_value("0x1F", "u8") == 31
    assert codec.parse_value("-7", "i16") == -7
    assert codec.parse_value("2.5e3", "f32") == 2500.0
    assert codec.parse_value("0x3FC00000", "f32") == 1.5   # bit pattern
    assert codec.encode(1.5, "f32", "little") == bytes.fromhex("0000C03F")


# --- memory ranges: RAM and flash banks listed by the user, over the ELF's sections ---------------

def h7_regions():
    """The read regions of an STM32H7 firmware, as start and inclusive end."""
    return [Range.from_dict({"name": n, "start": s, "end": e, "kind": "memory", **extra}) for n, s, e, extra in (
        ("ITCM", 0x00000000, 0x0000FFFF, {}),
        ("Flash", 0x08000000, 0x081FFFFF, {"writable": False}),
        ("DTCM", 0x20000000, 0x2001FFFF, {}),
        ("AXI SRAM", 0x24000000, 0x2407FFFF, {}),
    )]


@pytest.fixture
def banks():
    elf = load_elf_sections(FIRMWARE / "main.elf")   # .data and .bss sit at the start of DTCM
    return MemoryMap(elf.sections, h7_regions() + [Range("GPIOA", 0x48000000, 0x400)])


def test_memory_range_from_end_and_kind():
    r = h7_regions()[2]
    assert (r.start, r.size, r.kind) == (0x20000000, 0x20000, "memory")
    with pytest.raises(Exception, match="either size or end"):
        Range.from_dict({"name": "x", "start": 0, "size": 4, "end": 3})
    with pytest.raises(Exception, match="kind must be"):
        Range.from_dict({"name": "x", "start": 0, "size": 4, "kind": "ram"})


def test_memory_ranges_may_overlap_sections_and_extend_them(banks):
    banks.check(0x20000000, 0x20000)                 # all of DTCM, .data and .bss included
    banks.check(0x200000EA, 4)                       # past the end of .bss, still in DTCM
    banks.check(0x2001FFFC, 4, write=True)


@pytest.mark.parametrize("addr,size", [
    (0x2001FFFE, 4),                                 # starts in DTCM, runs off its end
    (0x0000FFF0, 0x20),                              # off the end of ITCM
    (0x20020000, 1),                                 # the gap after DTCM
])
def test_reads_past_a_memory_range_are_refused(banks, addr, size):
    with pytest.raises(AccessRefused, match="outside the MCU's known memory"):
        banks.check(addr, size)


def test_read_only_stays_read_only_inside_memory_ranges(banks):
    banks.check(0x08100000, 4)                       # flash with no section: readable
    with pytest.raises(AccessRefused, match="read-only Flash"):
        banks.check(0x08100000, 4, write=True)
    rw = MemoryMap(load_elf_sections(FIRMWARE / "main.elf").sections,
                   [Range("Flash", 0x08000000, 0x200000, kind="memory")])
    with pytest.raises(AccessRefused, match="read-only .text"):
        rw.check(0x08000000, 4, write=True)          # a writable range doesn't unlock .text


def test_a_peripheral_range_may_not_overlap_anything():
    sections = load_elf_sections(FIRMWARE / "main.elf").sections
    with pytest.raises(Exception, match="peripheral range DTCM overlaps"):
        MemoryMap(sections, [Range("DTCM", 0x20000000, 0x20000)])
    with pytest.raises(Exception, match="overlaps"):
        MemoryMap([], [Range("A", 0x1000, 16, kind="memory"), Range("P", 0x1008, 4)])


def test_scheduler_merges_inside_memory_ranges(banks):
    from rdt.scheduler import _readable
    assert _readable(banks, 0x20000000, 0x100)       # across .bss's end into the rest of DTCM
    assert not _readable(banks, 0x48000000, 8)       # a peripheral: never merged


# --- 4-byte aligned access (rani, 2026-10-12) ---------------------------------------------------

def test_aligned_padding_may_fall_outside_a_section():
    mm = MemoryMap([Area(".data", 0x20000000, 0x1E, True, False)])
    mm.check_aligned(0x2000001C, 2, 4)                 # the word runs 2 bytes past .data
    mm.check_aligned(0x2000001C, 2, 4, write=True)


def test_aligned_padding_never_reaches_into_a_peripheral():
    mm = MemoryMap([Area(".data", 0x20000000, 0x102, True, False)],
                   [Range("REG", 0x20000102, 2), Range("GPIOA", 0x48000000, 0x400)])
    with pytest.raises(AccessRefused, match="reaches into range REG"):
        mm.check_aligned(0x20000100, 2, 4)
    with pytest.raises(AccessRefused, match="crosses the edge of range REG"):
        mm.check_aligned(0x20000102, 2, 4)
    mm.check_aligned(0x48000002, 2, 4)                 # inside GPIOA: the word is in GPIOA too


def test_aligned_padding_is_not_written_into_read_only_memory():
    mm = MemoryMap([Area(".rodata", 0x20000000, 0x10, False, False), Area(".data", 0x20000010, 0x10, True, False)])
    with pytest.raises(AccessRefused, match="read-only .rodata"):
        mm.check_aligned(0x2000000E, 4, 4, write=True)
    mm.check_aligned(0x2000000E, 4, 4)                 # reading it is fine


def test_align_span():
    assert align_span(0x20000086, 2, 4) == (0x20000084, 0x20000088)
    assert align_span(0x20000084, 4, 4) == (0x20000084, 0x20000088)
    assert align_span(0x20000083, 2, 4) == (0x20000080, 0x20000088)
    assert align_span(0x20000083, 2, 1) == (0x20000083, 0x20000085)
