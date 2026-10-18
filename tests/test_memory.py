import pytest

from sim import Memory, MemoryFault, Region, Signal
from conftest import FIRMWARE, ram_mcu


def test_elf_loads_initialised_data_and_zeroed_bss():
    mem = Memory()
    assert mem.load_elf(FIRMWARE / "main.elf") == "little"
    # motor = {1.5f, 3000.0f, -120, 1, MODE_RUN}
    assert mem.read(0x20000000, 4) == bytes.fromhex("0000C03F")
    assert mem.read(0x20000008, 2) == (-120).to_bytes(2, "little", signed=True)
    assert mem.read(0x20000010, 4) == (0x12345678).to_bytes(4, "little")
    assert mem.read(0x20000024, 200) == bytes(200)  # rx_buffer in .bss
    assert mem.read(0x08000050, 9) == b"rdt-main\0"


def test_read_spans_adjacent_regions():
    mem = Memory()
    mem.load_elf(FIRMWARE / "main.elf")
    # gains[3] ends .data at 0x20000024, rx_buffer starts .bss there
    assert mem.read(0x20000020, 8) == (-40).to_bytes(4, "little", signed=True) + bytes(4)


def test_unmapped_and_read_only():
    mem = Memory()
    mem.load_elf(FIRMWARE / "main.elf")
    with pytest.raises(MemoryFault):
        mem.read(0x20000000 + 36 + 200, 1)
    with pytest.raises(MemoryFault):
        mem.write(0x08000050, b"x")
    mem.write(0x08000050, b"x", force=True)
    assert mem.read(0x08000050, 1) == b"x"


def test_reset_restores_the_image():
    mem = Memory()
    mem.add(Region("RAM", 0x100, bytearray(b"abcd"), True))
    mem.write(0x100, b"zz")
    mem.reset()
    assert mem.read(0x100, 4) == b"abcd"


def test_overlapping_regions_are_rejected():
    mem = Memory()
    mem.add(Region("A", 0x100, bytearray(16), True))
    with pytest.raises(ValueError):
        mem.add(Region("B", 0x108, bytearray(16), True))


def test_secondary_mcu_reads_in_frames_and_can_tear():
    t = iter(range(100))
    counters = [Signal(addr=0x20000000, type="u32", kind="counter", period_ms=1000),
                Signal(addr=0x20000040, type="u32", kind="counter", period_ms=1000)]
    mcu = ram_mcu(1, size=128, signals=counters, clock=lambda: float(next(t)))
    data = mcu.read(0x20000000, 128)
    assert mcu.frames == 2
    # Each 64-byte frame was taken at a different moment, as on the real internal channel.
    assert int.from_bytes(data[0:4], "little") == 0
    assert int.from_bytes(data[64:68], "little") == 1


def test_main_mcu_reads_in_one_go():
    mcu = ram_mcu(0, size=256)
    mcu.read(0x20000000, 256)
    assert mcu.frames == 0


@pytest.mark.parametrize("kind,t,expected", [
    ("counter", 2.5, 2), ("sine", 0.25, 1.0), ("square", 0.75, -1.0), ("sawtooth", 0.5, 0.0),
])
def test_signal_shapes(kind, t, expected):
    assert Signal(addr=0, kind=kind, type="f32").value(t) == pytest.approx(expected)


def test_signal_integer_wraps_like_c():
    s = Signal(addr=0, type="u8")
    assert s.encode(257, "little") == b"\x01"
    assert Signal(addr=0, type="i16").encode(-2, "big") == b"\xff\xfe"


def test_a_signal_outside_memory_is_a_config_error():
    # Every read refreshes every signal, so one at an unmapped address would break all reads.
    with pytest.raises(ValueError, match=r"MCU 1: signal tick at 0x200000FE \(4 bytes\) is not in its memory"):
        ram_mcu(1, size=256, signals=[Signal(addr=0x200000FE, type="u32", name="tick")])
