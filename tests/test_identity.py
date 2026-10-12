"""SYM-8: on connect, each MCU's ELF is compared with the firmware it runs."""

import io
import shutil
from pathlib import Path

import pytest

from rdt import McuConfig, WriteRefused
from rdt.cli import main
from rdt.memmap import build_id_note, elf_bytes
from sim import Simulator, TransportConfig, load_config

from host_helpers import FIRMWARE, make_target

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


def mcus(main_elf=FIRMWARE / "main.elf", identity="firmware_name"):
    return [McuConfig(0, "main", main_elf, identity=identity), McuConfig(1, "motorctl", FIRMWARE / "motorctl.elf")]


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
        assert [(m.name, m.identity.status) for m in t.mcus.values()] == [("main", "match"), ("motorctl", "unchecked")]
        assert "firmware_name matches main.elf" in str(t.mcus["main"].identity)
        t.write_value("main:gains[0]", 5)   # writes go through


def test_mismatch_refuses_writes_until_allowed(sim, tmp_path):
    with make_target("tcp", sim.tcp_address, mcus=mcus(other_build(tmp_path))) as t:
        ident = t.mcus["main"].identity
        assert ident.status == "mismatch"
        assert "device 'rdt-main', ELF 'rdt-mXin'" in ident.detail
        with pytest.raises(WriteRefused, match="does not match the firmware"):
            t.write_value("main:gains[0]", 5)
        with pytest.raises(WriteRefused):
            t.write("main", 0x20000000, b"\0")
        assert t.read_value("main:gains[0]") == 10       # reads still work
        t.write_value("@motorctl:0x20000084:u32", 7)    # the other MCU is not blocked
        t.allow_mismatch("main")
        t.write_value("main:gains[0]", 5)
        t.reconnect()                                    # checked again, and blocked again
        assert t.mcus["main"].identity.status == "mismatch" and not t.mcus["main"].allow_mismatch


def test_identity_problems_never_block(sim):
    with make_target("tcp", sim.tcp_address, mcus=mcus(identity="no_such_var")) as t:
        ident = t.mcus["main"].identity
        assert ident.status == "unchecked" and "no_such_var" in ident.detail
        t.write_value("main:gains[0]", 5)
    with make_target("tcp", sim.tcp_address, mcus=mcus(identity="rx_buffer")) as t:   # .bss: no value in the file
        assert t.mcus["main"].identity.status == "unchecked"
    cfgs = mcus()
    cfgs[1].identity = "pwm_duty"
    with make_target("tcp", sim.tcp_address, mcus=cfgs) as t:
        assert t.mcus["motorctl"].identity.status == "match"
        t.command("SIMulation:MCU:SILent 1,ON")
        ident = t.check_identity("motorctl")[0]
        assert ident.status == "unread" and "could not be read" in ident.detail
        t.write_value("main:gains[0]", 5)


def test_target_file_and_workspace_keep_identity(tmp_path):
    from rdt.workspace import Workspace
    ws = Workspace.load(ROOT / "examples" / "sim_bench.rdtw")
    assert ws.config.mcus[0].identity == "firmware_name" and ws.config.mcus[1].identity is None
    ws.save(tmp_path / "copy.rdtw")
    assert 'identity = "firmware_name"' in (tmp_path / "copy.rdtw").read_text()
    assert Workspace.load(tmp_path / "copy.rdtw").config.mcus[0].identity == "firmware_name"


def test_cli(sim, tmp_path, capsys):
    address = "%s:%d" % tuple(sim.tcp_address)
    out = io.StringIO()
    assert main(["-t", str(EXAMPLE_TARGET), "--tcp", address, "info"], out=out) == 0
    assert "identity: match, firmware_name matches main.elf" in out.getvalue()

    target = tmp_path / "target.toml"
    target.write_text(EXAMPLE_TARGET.read_text().replace(
        'profile = "', f'profile = "{(EXAMPLE_TARGET.parent).as_posix()}/').replace(
        '"../sim/examples/firmware/main.elf"', f'"{other_build(tmp_path).as_posix()}"').replace(
        '"../sim/examples/firmware/motorctl.elf"', f'"{(FIRMWARE / "motorctl.elf").as_posix()}"'))
    out = io.StringIO()
    assert main(["-t", str(target), "--tcp", address, "write", "main:gains[0]", "5"], out=out) == 1
    err = capsys.readouterr().err
    assert "warning: MCU main: firmware_name differs" in err and "--allow-mismatch" in err
    assert "writes are refused" in err
    out = io.StringIO()
    assert main(["-t", str(target), "--tcp", address, "--allow-mismatch", "write", "main:gains[0]", "5"], out=out) == 0
    assert "confirmed by read-back" in out.getvalue()
