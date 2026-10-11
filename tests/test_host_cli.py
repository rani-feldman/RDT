"""The rdt command line against the simulated device."""

import io
from pathlib import Path

import pytest

from rdt import TargetConfig
from rdt.cli import main
from sim import Simulator, TransportConfig, load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TARGET = ROOT / "examples" / "sim_target.toml"


@pytest.fixture
def sim():
    cfg = load_config(ROOT / "sim" / "examples" / "two_mcus.toml")
    cfg.device.mcus[0].signals.clear()   # stable values
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


def rdt(sim, *argv, transport="tcp"):
    address = "%s:%d" % (sim.tcp_address if transport == "tcp" else sim.udp_address)
    out = io.StringIO()
    code = main(["-t", str(EXAMPLE_TARGET), f"--{transport}", address, *argv], out=out)
    return code, out.getvalue()


def test_example_target_file():
    cfg = TargetConfig.load(EXAMPLE_TARGET)
    assert [(m.id, m.name, m.elf.exists()) for m in cfg.mcus] == [(0, "main", True), (1, "motorctl", True)]
    assert cfg.connection.address == "127.0.0.1:5025"
    assert cfg.profile.name == "RDT simulator"


def test_info(sim):
    code, out = rdt(sim, "info")
    assert code == 0
    assert "RDT,Simulator" in out
    assert "MCU 1 motorctl (motorctl.elf, little endian)" in out
    assert "0x48000000      1024  rw  range    GPIOA" in out


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_read_and_write(sim, transport):
    code, out = rdt(sim, "read", "@main:0x20000000:f32", "@motorctl:0x20000084:u32", transport=transport)
    assert code == 0
    assert out.splitlines() == ["@main:0x20000000:f32 = 1.5", "@motorctl:0x20000084:u32 = 500 (0x000001F4)"]
    code, out = rdt(sim, "write", "@main:0x20000004:f32", "2500", transport=transport)
    assert (code, out.strip()) == (0, "@main:0x20000004:f32 = 2500 (confirmed by read-back)")
    assert rdt(sim, "read", "@main:0x20000004:f32")[1].strip() == "@main:0x20000004:f32 = 2500"


def test_read_reports_each_failure_and_exits_1(sim):
    code, out = rdt(sim, "read", "@main:0x30000000:u32", "@main:0x20000010:u32")
    assert code == 1
    assert "outside the MCU's known memory" in out
    assert "@main:0x20000010:u32 = 305419896 (0x12345678)" in out


def test_write_refuses_bad_values(sim, capsys):
    assert rdt(sim, "write", "@main:0x20000014:u8", "300")[0] == 1
    assert "does not fit in u8" in capsys.readouterr().err
    assert rdt(sim, "write", "@main:0x08000000:u32", "1")[0] == 1
    assert "read-only .text" in capsys.readouterr().err


def test_dump_and_poke(sim):
    code, out = rdt(sim, "poke", "main", "0x20000024", "48656C6C6F")
    assert code == 0
    code, out = rdt(sim, "dump", "main", "0x20000020", "16")
    assert out.strip() == "20000020  D8 FF FF FF 48 65 6C 6C 6F 00 00 00 00 00 00 00  |....Hello.......|"


def test_send_and_errors(sim):
    assert rdt(sim, "send", "*IDN?")[1].startswith("RDT,Simulator")
    code, out = rdt(sim, "send", "MEM:READ?", "0,0x30000000,4")
    assert code == 1 and out.startswith('(no reply; error queue: -222,"Data out of range')
    rdt(sim, "send", "FOO", "1")
    rdt(sim, "send", "BAR", "2")
    code, out = rdt(sim, "errors")
    assert out.splitlines() == ['-113,"Undefined header;FOO"', '-113,"Undefined header;BAR"', '0,"No error"']


def test_silent_mcu(sim):
    rdt(sim, "send", "SIM:MCU:SIL", "1,ON")
    code, out = rdt(sim, "read", "@motorctl:0x20000084:u32", "@main:0x20000010:u32")
    assert code == 1
    assert 'MCU motorctl is not responding (-240,"MCU not responding")' in out
    assert "@main:0x20000010:u32 = 305419896" in out


def test_watch(sim):
    code, out = rdt(sim, "watch", "@main:0x20000010:u32", "@motorctl:0x20000088:f32", "-i", "20", "-n", "3")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 4
    assert "@main:0x20000010:u32" in lines[0]
    assert lines[1].split()[1] == "305419896"


def test_console(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("*IDN?\nNOPE?\n.err\n.read @main:0x20000000:f32\n.quit\n"))
    code, out = rdt(sim, "console")
    assert code == 0
    lines = out.splitlines()
    assert lines[1].startswith("RDT,Simulator")
    assert lines[2] == '(no reply; error queue: -113,"Undefined header;NOPE?")'
    assert lines[3] == '0,"No error"'
    assert lines[4] == "@main:0x20000000:f32 = 1.5"


def test_mcus_on_the_command_line(sim):
    out = io.StringIO()
    elf = ROOT / "sim" / "examples" / "firmware" / "motorctl.elf"
    code = main(["--tcp", "%s:%d" % sim.tcp_address, "--mcu", "main=0", "--mcu", f"motor=1:{elf}",
                 "read", "@motor:0x20000084:u32"], out=out)
    assert (code, out.getvalue().strip()) == (0, "@motor:0x20000084:u32 = 500 (0x000001F4)")


def test_no_mcus_is_an_error(capsys):
    assert main(["--tcp", "127.0.0.1:1", "info"]) == 1
    assert "no MCUs" in capsys.readouterr().err


def test_symbol_names_are_not_yet_supported(sim, capsys):
    assert rdt(sim, "read", "main:motor.speed")[0] == 1
    assert "@mcu:address:type" in capsys.readouterr().err
