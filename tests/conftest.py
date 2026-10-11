from pathlib import Path

import pytest

from sim import Device, Memory, Mcu, Profile, Region

FIRMWARE = Path(__file__).resolve().parent.parent / "sim" / "examples" / "firmware"


@pytest.fixture
def two_mcus():
    """MCU 0 and MCU 1 loaded from the example ELFs, with no signals so reads are stable."""
    mcus = []
    for mcu_id, name in ((0, "main"), (1, "motorctl")):
        mem = Memory()
        order = mem.load_elf(FIRMWARE / f"{name}.elf")
        mcus.append(Mcu(mcu_id, name, mem, order))
    return mcus


@pytest.fixture
def device(two_mcus):
    return Device(two_mcus, Profile(), internal_timeout_ms=1)


def ram_mcu(mcu_id=0, start=0x20000000, size=256, **kw):
    mem = Memory()
    mem.add(Region("RAM", start, bytearray(size), True))
    return Mcu(mcu_id, f"mcu{mcu_id}", mem, **kw)
