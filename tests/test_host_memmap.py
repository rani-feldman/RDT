import pytest

from rdt import AccessRefused, codec
from rdt.memmap import MemoryMap, load_elf_sections
from rdt.profile import Range
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
