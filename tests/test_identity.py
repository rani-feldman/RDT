"""SYM-8: on connect, each MCU's ELF is compared with the firmware it runs."""

import io
import shutil
import time
from pathlib import Path

import pytest
from host_helpers import FIRMWARE, Relay, make_target

from rdt import ImageMismatch, McuConfig, WriteRefused
from rdt.cli import main
from rdt.memmap import build_id_note, elf_bytes, flash_samples
from sim import Simulator, TransportConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TARGET = ROOT / "examples" / "sim_target.toml"


@pytest.fixture
def sim():
    cfg = load_config(ROOT / "sim" / "examples" / "two_mcus.toml")
    cfg.device.mcus[0].signals.clear()
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


def other_build(tmp_path) -> Path:
    """A copy of main.elf as if rebuilt with another version string: the firmware still says
    rdt-main, the ELF says rdt-mXin."""
    elf = tmp_path / "main.elf"
    data = (FIRMWARE / "main.elf").read_bytes()
    assert data.count(b"rdt-main\0") == 1
    elf.write_bytes(data.replace(b"rdt-main\0", b"rdt-mXin\0"))
    return elf


def mcus(main_elf=FIRMWARE / "main.elf"):
    return [McuConfig(0, "main", main_elf), McuConfig(1, "motorctl", FIRMWARE / "motorctl.elf")]


def test_elf_bytes():
    assert elf_bytes(FIRMWARE / "main.elf", 0x20000000, 4) is not None   # .data holds its initial value
    assert elf_bytes(FIRMWARE / "main.elf", 0x20000100, 4) is None       # .bss is not in the file
    assert build_id_note(FIRMWARE / "main.elf") is None


def test_build_id_note_of_a_host_program():
    host = Path("/bin/ls")
    if not host.exists():
        pytest.skip("no host ELF")
    note = build_id_note(host)
    if note is None:
        pytest.skip("the host's ls has no build ID")
    addr, size = note
    assert size > 16 and elf_bytes(host, addr, size)[12:16] == b"GNU\0"


def test_matching_elf(sim):
    with make_target("tcp", sim.tcp_address, mcus=mcus()) as t:
        assert [(m.name, m.identity.status) for m in t.mcus.values()] == [("main", "match"), ("motorctl", "match")]
        assert "2 flash blocks (88 bytes) match motorctl.elf" in str(t.mcus["motorctl"].identity)
        assert "3 flash blocks (112 bytes) match main.elf" in str(t.mcus["main"].identity)
        t.write_value("main:gains[0]", 5)   # writes go through


def test_mismatch_refuses_writes_until_allowed(sim, tmp_path):
    with make_target("tcp", sim.tcp_address, mcus=mcus(other_build(tmp_path))) as t:
        ident = t.mcus["main"].identity
        assert ident.status == "mismatch"
        assert "1 of 3 flash blocks (112 bytes) differ" in ident.detail
        with pytest.raises(WriteRefused, match="does not match the firmware"):
            t.write_value("main:gains[0]", 5)
        with pytest.raises(WriteRefused):
            t.write("main", 0x20000000, b"\0")
        with pytest.raises(ImageMismatch, match="reads and writes are refused"):
            t.read_value("main:gains[0]")                # reads are refused too
        t.write_value("@motorctl:0x20000084:u32", 7)    # the other MCU is not blocked
        t.allow_mismatch("main")
        assert t.read_value("main:gains[0]") == 10
        t.write_value("main:gains[0]", 5)
        t.reconnect()                                    # checked again, and blocked again
        assert t.mcus["main"].identity.status == "mismatch" and not t.mcus["main"].allow_mismatch


def test_build_id_in_flash_on_the_h7(tmp_path):
    """An STM32H7 build with the GNU build ID kept in flash, read in aligned 4-byte words."""
    from test_stm32h7 import SIM_CONFIG, WORKSPACE, run, with_elf

    from rdt.workspace import Workspace

    flashed = FIRMWARE / "main_build_id.elf"
    assert build_id_note(flashed) == (0x08000000, 36)
    sim_config = with_elf(tmp_path, SIM_CONFIG.read_text(), flashed)
    for elf, status in ((flashed, "match"), (FIRMWARE / "main_build_id_v2.elf", "mismatch")):
        ws = Workspace.load(WORKSPACE)
        ws.config.mcus[0].elf = elf
        with run(sim_config, ws) as t:
            assert t.align == 4
            ident = t.mcus["Interface"].identity
            assert ident.status == status
            if status == "mismatch":   # shown as the hash, as readelf -n prints it
                assert ("device 838ca2732e15283c254448c2f0dc0131c0616be4, "
                        "ELF 0d8ac456d8300cb502ab6131ad1bc35a8fb68e8f") in ident.detail
                with pytest.raises(WriteRefused):
                    t.write_value("Interface:gains[0]", 5)
            else:
                t.write_value("Interface:gains[0]", 5)


def test_checked_again_after_the_link_comes_back(sim):
    """The firmware may be flashed while the link is down: the next write checks again first."""
    relay = Relay(sim.tcp_address)
    try:
        t = make_target("tcp", relay.address, mcus=mcus())
        t.session.RECONNECT_DELAYS = (0.05, 0.1)
        with t:
            assert t.mcus["main"].identity.status == "match" and t.stale_identities() == []
            relay.cut()
            with pytest.raises(Exception):
                t.read_value("main:gains[0]")
            addr = t.resolve("main:firmware_name").addr
            sim.device.mcus[0].memory.write(addr, b"rdt-new\0", force=True)   # flashed meanwhile
            assert t.stale_identities() == []                                 # nothing to check while down
            relay.heal()
            assert t.session.wait_until_up(3)
            assert t.stale_identities() == ["main", "motorctl"]
            with pytest.raises(WriteRefused, match="does not match"):
                t.write_value("main:gains[0]", 5)
            assert t.stale_identities() == ["motorctl"] and "differ" in t.mcus["main"].identity.detail
    finally:
        relay.close()


def test_flash_samples_without_a_build_id(sim, tmp_path):
    """No build ID: blocks of the read-only sections are compared (SYM-8)."""
    samples = flash_samples(FIRMWARE / "main.elf")
    assert [(hex(a), len(b)) for a, b in samples] == [("0x8000000", 80), ("0x8000050", 16), ("0x8000060", 16)]
    data = bytearray((FIRMWARE / "main.elf").read_bytes())
    at = data.index(samples[0][1][:16]) + 8                 # a byte of .text in the file
    data[at] ^= 0xFF
    elf = tmp_path / "main.elf"
    elf.write_bytes(bytes(data))
    with make_target("tcp", sim.tcp_address, mcus=mcus(elf)) as t:
        ident = t.mcus["main"].identity
        assert ident.status == "mismatch"
        assert "1 of 3 flash blocks (112 bytes) differ, first at 0x08000008" in ident.detail
        with pytest.raises(ImageMismatch):
            t.read("main", 0x20000000, 4)
    big = flash_samples("/bin/ls")
    if big:                                                    # a large image: about 4 KB, in words
        assert 2000 < sum(len(b) for _, b in big) < 8000 and all(a % 4 == 0 and len(b) % 4 == 0 for a, b in big)


def keil_like_elf(path: Path, one_segment: bool = False) -> Path:
    """A tiny ARM ELF laid out as a Keil scatter file does it: ER_IROM1 runs from flash at
    0x08000000, ER_ITCM runs from ITCM at 0x00000000 but is flashed right after it, at
    0x08000040, and copied there at startup. Its own segment's physical address is the load
    address; or, as armlink also does (``one_segment``), there is only the flash segment, which
    holds both sections' bytes."""
    import struct
    rom, itcm = bytes(range(64)), bytes(range(100, 132))
    shstr = b"\0ER_IROM1\0ER_ITCM\0.shstrtab\0"
    ehsize, phsize, shsize = 52, 32, 40
    off_rom = ehsize + 2 * phsize
    off_itcm = off_rom + len(rom)
    off_str = off_itcm + len(itcm)
    off_sh = off_str + len(shstr)
    off_sh += -off_sh % 4
    out = bytearray(b"\x7fELF\x01\x01\x01" + bytes(9))
    segs = ([(off_rom, rom + itcm, 0x08000000, 0x08000000)] if one_segment else
            [(off_rom, rom, 0x08000000, 0x08000000), (off_itcm, itcm, 0, 0x08000040)])
    out += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0x08000001, ehsize, off_sh, 0x05000000,
                       ehsize, phsize, 2, shsize, 4, 3)
    segs += [(0, b"", 0, 0)] * (2 - len(segs))      # an empty PT_NULL keeps the header count
    for off, data, vaddr, paddr in segs:
        out += struct.pack("<IIIIIIII", 1 if data else 0, off, vaddr, paddr, len(data), len(data), 5, 4)
    out += rom + itcm + shstr
    out += bytes(off_sh - len(out))
    out += bytes(shsize)
    flags = 2 | 4                                                    # SHF_ALLOC | SHF_EXECINSTR
    for name, off, data, addr in ((1, off_rom, rom, 0x08000000), (10, off_itcm, itcm, 0)):
        out += struct.pack("<IIIIIIIIII", name, 1, flags, addr, off, len(data), 0, 0, 4, 0)
    out += struct.pack("<IIIIIIIIII", 18, 3, 0, 0, off_str, len(shstr), 0, 0, 1, 0)
    path.write_bytes(bytes(out))
    return path


def test_flash_samples_skip_code_copied_to_itcm(tmp_path):
    """Code a Keil scatter file runs from ITCM or RAM is flashed elsewhere and copied at startup,
    so it is not sampled at its run address: that gave a false mismatch on a matching image."""
    samples = flash_samples(keil_like_elf(tmp_path / "keil.axf"))
    assert samples == [(0x08000000, bytes(range(64)))]
    # armlink with one segment for all of flash: ITCM code at 0x0 sits in it, at 0x08000040 (rani's
    # Control_CM7: ER_IROM2 at 0x00000000 in the 0x08040000 segment; 8 ITCM blocks "differed")
    samples = flash_samples(keil_like_elf(tmp_path / "keil1.axf", one_segment=True))
    assert samples == [(0x08000000, bytes(range(64)))]


def test_identity_problems_never_block(sim):
    with make_target("tcp", sim.tcp_address, mcus=mcus()) as t:
        assert t.mcus["motorctl"].identity.status == "match"
        t.command("SIMulation:MCU:SILent 1,ON")
        ident = t.check_identity("motorctl")[0]
        assert ident.status == "unread" and "could not be read" in ident.detail
        t.write_value("main:gains[0]", 5)


def test_old_identity_variable_is_ignored(tmp_path):
    """The per-MCU identity variable was removed (rani, 2026-10-15): an old file that has one still
    loads, the flash sample is compared, and the key is dropped on save."""
    import tomllib

    from rdt.workspace import Workspace
    bench = ROOT / "examples" / "sim_bench.rdtw"
    raw = tomllib.loads(bench.read_text())
    raw["mcu"][0]["identity"] = "firmware_name"
    ws = Workspace.from_dict(raw, bench.parent)
    ws.save(tmp_path / "copy.rdtw")
    assert "identity" not in (tmp_path / "copy.rdtw").read_text()


def test_cli(sim, tmp_path, capsys):
    address = "%s:%d" % tuple(sim.tcp_address)
    out = io.StringIO()
    assert main(["-t", str(EXAMPLE_TARGET), "--tcp", address, "info"], out=out) == 0
    assert "identity: match, 3 flash blocks (112 bytes) match main.elf" in out.getvalue()

    target = tmp_path / "target.toml"
    target.write_text(EXAMPLE_TARGET.read_text().replace(
        'profile = "', f'profile = "{(EXAMPLE_TARGET.parent).as_posix()}/').replace(
        '"../sim/examples/firmware/main.elf"', f'"{other_build(tmp_path).as_posix()}"').replace(
        '"../sim/examples/firmware/motorctl.elf"', f'"{(FIRMWARE / "motorctl.elf").as_posix()}"'))
    out = io.StringIO()
    assert main(["-t", str(target), "--tcp", address, "write", "main:gains[0]", "5"], out=out) == 1
    err = capsys.readouterr().err
    assert "warning: MCU main: flash differs from main.elf: 1 of 3 flash blocks" in err and "--allow-mismatch" in err
    assert "writes are refused" in err
    out = io.StringIO()
    assert main(["-t", str(target), "--tcp", address, "--allow-mismatch", "write", "main:gains[0]", "5"], out=out) == 0
    assert "confirmed by read-back" in out.getvalue()
