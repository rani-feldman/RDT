"""The single-MCU STM32H7 example: simulator config and RDT workspace, with and without an ELF."""

import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from rdt import AccessRefused, Target
from rdt.workspace import Workspace
from sim import Simulator, TransportConfig, load_config
from sim.memory import Memory, MemoryFault, Region

from host_helpers import FIRMWARE

ROOT = Path(__file__).resolve().parent.parent
SIM_CONFIG = ROOT / "sim" / "examples" / "stm32h7_interface.toml"
WORKSPACE = ROOT / "examples" / "stm32h7_interface.rdtw"


@contextmanager
def run(sim_config, ws):
    """The simulator from ``sim_config``, and RDT connected to it with the workspace's target."""
    cfg = load_config(sim_config)
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp=None)) as sim:
        ws.config.connection.address = "%s:%d" % tuple(sim.tcp_address)
        ws.config.profile.timeout_ms = 300
        with Target(ws.config) as t:
            yield t


def with_elf(tmp_path, text, elf):
    """A copy of ``text`` with the commented-out elf line pointing at ``elf``."""
    out = tmp_path / "with_elf.toml"
    text = text.replace('# elf = "C:/work/firmware/build/interface.elf"', f'elf = "{elf.as_posix()}"')
    text = text.replace('profile             = "default_profile.toml"',
                        f'profile = "{(SIM_CONFIG.parent / "default_profile.toml").as_posix()}"')
    out.write_text(text)
    return out


def test_banks_without_an_elf():
    ws = Workspace.load(WORKSPACE)
    assert [m.name for m in ws.config.mcus] == ["Interface"]
    with run(SIM_CONFIG, ws) as t:
        first = t.read_value("@Interface:0x24000000:u32")
        time.sleep(0.02)
        assert t.read_value("@Interface:0x24000000:u32") > first     # the uptime_ms counter
        assert t.read("Interface", 0x08000000, 8) == b"\xff" * 8     # erased flash
        for addr in (0x00000000, 0x2001FFFC, 0x24040000, 0x30047FFC, 0x38000010, 0x38800FFC):
            t.write_value(f"@Interface:0x{addr:08X}:u32", 0x12345678)
            assert t.read_value(f"@Interface:0x{addr:08X}:u32") == 0x12345678
        with pytest.raises(AccessRefused):
            t.write_value("@Interface:0x08000000:u32", 1)            # flash is read-only
        with pytest.raises(AccessRefused):
            t.read("Interface", 0x24080000, 4)                       # past the end of AXI SRAM
        _, rows = ws.read(t, ws.watches[2])
        assert rows[0].value == 0xFFFFFFFF


def test_elf_sections_are_laid_over_the_banks(tmp_path):
    sim_config = with_elf(tmp_path, SIM_CONFIG.read_text(), FIRMWARE / "main.elf")
    ws = Workspace.load(WORKSPACE)
    ws.config.mcus[0].elf = FIRMWARE / "main.elf"
    regions = {(r.name, r.start) for r in load_config(sim_config).device.mcus[0].memory.regions}
    assert ("DTCM", 0x20000024 + 200) in regions and (".data", 0x20000000) in regions
    with run(sim_config, ws) as t:
        assert t.read_value("Interface:gains[1]") == -20                   # .data initial value
        assert t.read_value("Interface:firmware_name").startswith(b"rdt-")  # .rodata, inside flash
        assert t.read("Interface", 0x08100000, 4) == b"\xff" * 4           # the rest of flash
        t.write_value("Interface:motor.target", 1234.0)
        assert t.read_value("Interface:motor.target") == 1234.0
        t.write_value("@Interface:0x20010000:u32", 7)                       # DTCM past .bss
        assert t.read_value("@Interface:0x20010000:u32") == 7


def test_memory_banks_split_around_a_section():
    m = Memory()
    m.add(Region("RAM", 0x100, bytearray(16), True, backdrop=True))
    m.add(Region(".text", 0x104, bytearray(b"\xaa" * 4), False))
    assert [(r.name, r.start, len(r.data)) for r in m.regions] == [("RAM", 0x100, 4), (".text", 0x104, 4),
                                                                   ("RAM", 0x108, 8)]
    assert m.read(0x100, 12) == bytes(4) + b"\xaa" * 4 + bytes(4)
    with pytest.raises(MemoryFault):
        m.write(0x104, b"\x00")
    m.write(0x10C, b"\x01")
    with pytest.raises(ValueError, match="overlaps"):
        m.add(Region("other", 0x106, bytearray(4), True))
