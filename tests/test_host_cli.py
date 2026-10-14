"""The rdt command line against the simulated device."""

import io
import re
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
    assert "Link:          tcp 127.0.0.1:" in out and "Device:" not in out
    assert "MCU 1 motorctl (motorctl.elf, little endian)" in out
    assert "0x48000000      1024  rw  peripheral  GPIOA" in out


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


def test_send(sim):
    assert rdt(sim, "send", "*IDN?")[1].startswith("RDT,Simulator")
    code, out = rdt(sim, "send", "MEM:READ?", "0,30000000,4")
    assert code == 1 and out.strip() == "(no reply)"
    rdt(sim, "send", "FOO", "1")
    code, out = rdt(sim, "send", "SYST:ERR?")         # the error queue is still there to ask by hand
    assert out.strip().startswith("-222,")


def test_silent_mcu(sim):
    rdt(sim, "send", "SIM:MCU:SIL", "1,ON")
    code, out = rdt(sim, "read", "@motorctl:0x20000084:u32", "@main:0x20000010:u32")
    assert code == 1
    assert "MCU motorctl is not responding (3 timeouts in a row)" in out
    assert "@main:0x20000010:u32 = 305419896" in out


def test_watch(sim):
    code, out = rdt(sim, "watch", "@main:0x20000010:u32", "@motorctl:0x20000088:f32", "-i", "20", "-n", "3")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 4
    assert "@main:0x20000010:u32" in lines[0]
    assert lines[1].split()[1] == "305419896"


def test_watch_logs_csv(sim, tmp_path):
    log = tmp_path / "w.csv"
    code, out = rdt(sim, "watch", "main:motor", "@main:0x20000010:u32", "-i", "20", "-n", "3", "--csv", str(log))
    assert code == 0 and out.splitlines()[-1] == f"# 3 rows logged to {log}"
    rows = log.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0] == ("time,seconds,main:motor.speed,main:motor.target,main:motor.current_ma,main:motor.enabled,"
                       "main:motor.mode,@main:0x20000010:u32,event")
    assert rows[1].split(",")[1:] == ["0.000", "1.5", "3000", "-120", "1", "1", "305419896", ""]
    assert len(rows) == 4


def test_console(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("*IDN?\nNOPE?\n.read @main:0x20000000:f32\n.quit\n"))
    code, out = rdt(sim, "console")
    assert code == 0
    lines = out.splitlines()
    assert lines[1].startswith("RDT,Simulator")
    assert lines[2] == "(no reply)"
    assert lines[3] == "@main:0x20000000:f32 = 1.5"


def test_mcus_on_the_command_line(sim, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)        # no rdt.toml here
    out = io.StringIO()
    elf = ROOT / "sim" / "examples" / "firmware" / "motorctl.elf"
    code = main(["--tcp", "%s:%d" % sim.tcp_address, "--mcu", "main=0", "--mcu", f"motor=1:{elf}",
                 "read", "@motor:0x20000084:u32"], out=out)
    assert (code, out.getvalue().strip()) == (0, "@motor:0x20000084:u32 = 500 (0x000001F4)")


def test_no_mcus_is_an_error(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)        # no rdt.toml here
    assert main(["--tcp", "127.0.0.1:1", "info"]) == 1
    assert "no MCUs" in capsys.readouterr().err


def test_rdt_toml_in_the_current_folder_is_the_default(sim, tmp_path, monkeypatch):
    (tmp_path / "rdt.toml").write_text(EXAMPLE_TARGET.read_text().replace('"../', f'"{ROOT.as_posix()}/'))
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    code = main(["--tcp", "%s:%d" % sim.tcp_address, "read", "main:motor.target"], out=out)
    assert code == 0 and out.getvalue().startswith("main:motor.target = ")


def test_read_by_name(sim):
    code, out = rdt(sim, "read", "main:motor.speed", "motorctl:pwm_duty", "main:firmware_name")
    assert code == 0
    assert out.splitlines() == ['main:motor.speed = 1.5', 'motorctl:pwm_duty = 500 (0x000001F4)',
                                'main:firmware_name = "rdt-main"']


def test_read_a_struct(sim):
    code, out = rdt(sim, "read", "main:motor")
    assert code == 0
    assert out.splitlines() == [
        "main:motor  motor_t, 16 bytes at 0x20000000",
        "main:motor.speed      = 1.5            float",
        "main:motor.target     = 3000           float",
        "main:motor.current_ma = -120 (0xFF88)  int16_t",
        "main:motor.enabled    = 1 (0x01)       uint8_t",
        "main:motor.mode       = MODE_RUN (1)   mode_t",
    ]


def test_read_a_large_struct_on_a_secondary_mcu(sim):
    # 132 bytes behind MCU 0, so split into 64-byte frames, and its array cut at --max-cells.
    code, out = rdt(sim, "read", "motorctl:adc_log", "--max-cells", "4")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "motorctl:adc_log  adc_log_t, 132 bytes at 0x20000000"
    assert lines[1].startswith("motorctl:adc_log.samples[0]") and "= 1 (0x0001)" in lines[1]
    assert lines[5] == "motorctl:adc_log.samples[4..63]   ... 60 more cells"
    assert lines[6].startswith("motorctl:adc_log.count ")   # a counter in the simulator


def test_write_by_name(sim):
    assert rdt(sim, "write", "main:motor.mode", "MODE_FAULT") == (
        0, "main:motor.mode = MODE_FAULT (2) (confirmed by read-back)\n")
    assert rdt(sim, "write", "main:gains[3]", "-7")[1] == "main:gains[3] = -7 (0xFFFFFFF9) (confirmed by read-back)\n"
    assert rdt(sim, "read", "main:gains[3]", "main:motor.mode")[1].splitlines() == [
        "main:gains[3] = -7 (0xFFFFFFF9)", "main:motor.mode = MODE_FAULT (2)"]


def test_write_by_name_refuses(sim, capsys):
    assert rdt(sim, "write", "main:motor.mode", "MODE_FAST")[0] == 1
    assert "use one of MODE_IDLE, MODE_RUN, MODE_FAULT" in capsys.readouterr().err
    assert rdt(sim, "write", "main:motor", "1")[0] == 1
    assert "write one field or cell at a time" in capsys.readouterr().err
    assert rdt(sim, "write", "main:firmware_name[0]", "'x'")[0] == 1
    assert "read-only .rodata" in capsys.readouterr().err


def test_name_errors(sim):
    code, out = rdt(sim, "read", "main:nope", "main:gains[4]", "motor:speed", "main:motor.speed")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "main:nope : no variable 'nope' in main.elf"
    assert lines[1] == "main:gains[4] : gains[4] is out of range; gains has 4 cells (0..3)"
    assert lines[2] == "motor:speed : unknown MCU 'motor'; this target has main, motorctl"
    assert lines[3] == "main:motor.speed = 1.5"


def test_dump_by_name(sim):
    code, out = rdt(sim, "dump", "main:firmware_name")
    assert out.strip() == "08000050  72 64 74 2D 6D 61 69 6E 00 00 00 00 00 00 00 00  |rdt-main........|"
    assert rdt(sim, "dump", "main:firmware_name", "4")[1].strip().startswith("08000050  72 64 74 2D  ")


def test_symbols_and_ptype_need_no_device(capsys):
    out = io.StringIO()
    # Port 1 refuses connections: these commands never open the link.
    assert main(["-t", str(EXAMPLE_TARGET), "--tcp", "127.0.0.1:1", "symbols", "main"], out=out) == 0
    assert out.getvalue().splitlines() == [
        "0x08000050      16  const char[16]     firmware_name",
        "0x20000000      16  motor_t            motor",
        "0x20000010       4  volatile uint32_t  tick_count",
        "0x20000014      16  int32_t[4]         gains",
        "0x20000024     200  uint8_t[200]       rx_buffer",
    ]
    out = io.StringIO()
    assert main(["-t", str(EXAMPLE_TARGET), "--tcp", "127.0.0.1:1", "ptype", "main:motor"], out=out) == 0
    text = out.getvalue()
    assert text.splitlines()[0] == "main:motor  motor_t  struct {...}, 16 bytes at 0x20000000 in main.c"
    assert "+0x0C  mode        mode_t" in text and "{MODE_IDLE = 0, MODE_RUN = 1, MODE_FAULT = 2}" in text
    out = io.StringIO()
    assert main(["-t", str(EXAMPLE_TARGET), "--tcp", "127.0.0.1:1", "ptype", "main:mode_t"], out=out) == 0
    assert out.getvalue().startswith("mode_t  enum {...}, 4 bytes")


def test_watch_by_name(sim):
    code, out = rdt(sim, "watch", "main:motor", "motorctl:pwm_duty", "-i", "20", "-n", "2")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 3
    assert lines[0].split() == ["time", "main:motor.speed", "main:motor.target", "main:motor.current_ma",
                                "main:motor.enabled", "main:motor.mode", "motorctl:pwm_duty"]
    assert lines[1].split()[1:] == ["1.5", "3000", "-120", "1", "MODE_RUN", "500"]


def test_console_by_name(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(".write main:motor.enabled 0\n.read main:motor.enabled\n.quit\n"))
    code, out = rdt(sim, "console")
    assert out.splitlines()[1:] == ["main:motor.enabled = 0 (0x00) (confirmed by read-back)",
                                    "main:motor.enabled = 0 (0x00)"]


def test_console_history(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("*IDN?\nMEM:MAX?\n.read @main:0x20000000:f32\n.history\n!1\n"
                                                 ".history 1\n!9\n.quit\n"))
    code, out = rdt(sim, "console")
    lines = out.splitlines()
    assert lines[4:6] == ["   1  *IDN?", "   2  MEM:MAX?"]      # only SCPI commands are kept
    assert lines[6] == "> *IDN?" and lines[7].startswith("RDT,Simulator")
    assert lines[8] == "   2  *IDN?"                           # sent again: now the newest
    assert lines[9] == "error: no history entry '9'; .history lists them"
    # kept for the next session, and for `rdt history`
    code, out = rdt(sim, "history")
    assert (code, out.splitlines()) == (0, ["   1  MEM:MAX?", "   2  *IDN?"])
    assert main(["history", "--clear"], out=io.StringIO()) == 0
    out = io.StringIO()
    assert main(["history"], out=out) == 0 and out.getvalue() == "(the console history is empty)\n"


def test_console_timestamps(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("*IDN?\n.quit\n"))
    code, out = rdt(sim, "console", "--timestamps")
    lines = out.splitlines()
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}  > \*IDN\?", lines[1])
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}  RDT,Simulator.*", lines[2])


def test_console_log(sim, monkeypatch, tmp_path):
    log = tmp_path / "console.txt"
    monkeypatch.setattr("sys.stdin", io.StringIO("*IDN?\nNOPE?\n.quit\n"))
    code, out = rdt(sim, "console", "--log", str(log))
    assert code == 0
    lines = [re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ", "", line)
             for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == "# log started"
    assert lines[1].startswith("# Connected to tcp 127.0.0.1:")
    assert lines[2] == "> *IDN?" and lines[3].startswith("< RDT,Simulator")
    assert lines[4] == "> NOPE?"
    assert lines[5] == "! (no reply)"
    assert lines[6:] == ["> .quit", "# log stopped"]


def test_send_expect(sim, capsys):
    code, out = rdt(sim, "send", "*IDN?", "--expect", r"^RDT,Sim")
    assert code == 0 and out.startswith("RDT,Simulator")
    code, out = rdt(sim, "send", "*IDN?", "--expect", "Keysight")
    assert code == 11 and "does not match 'Keysight'" in capsys.readouterr().err
    code, out = rdt(sim, "send", "NOPE?", "--expect", ".")
    assert code == 11 and out == "(no reply)\n"
    assert rdt(sim, "send", "*IDN?", "--expect", "(")[0] == 1                 # bad regex
    assert rdt(sim, "send", "*IDN?", "--expect", "x", "--no-reply")[0] == 1


def test_console_info_reconnect_break(sim, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(".info\n.reconnect\n*IDN?\n.break\n.quit\n"))
    code, out = rdt(sim, "console")
    lines = out.splitlines()
    assert lines[1].startswith("Link:") and any(line.startswith("MCU 1 motorctl") for line in lines)
    i = next(i for i, line in enumerate(lines) if line.startswith("reconnected to tcp 127.0.0.1:"))
    assert lines[i + 1].startswith("RDT,Simulator")
    assert lines[i + 2] == "error: tcp %s:%d: a break can only be sent on a COM port" % sim.tcp_address


def test_commands(sim, tmp_path, monkeypatch):
    ws = tmp_path / "bench.rdtw"
    assert main(["-t", str(EXAMPLE_TARGET), "init", str(ws)], out=io.StringIO()) == 0
    src = tmp_path / "fw.csv"
    src.write_text("group,command,description\nMotor,MOTor:SPEed?,Speed in rpm\nMotor,MOTor:STOP,\n"
                   ",*IDN?,Identify\n", encoding="utf-8")

    def run(*argv):
        out = io.StringIO()
        return main(["-w", str(ws), *argv], out=out), out.getvalue()

    assert run("commands", "import", str(src)) == (0, f"imported 3 commands in {ws}\n")
    txt = tmp_path / "more.txt"
    txt.write_text("# extra\n*idn?\nCONF:MODE 1\n\n", encoding="utf-8")
    assert run("commands", "import", str(txt)) == (0, f"imported 1 commands, skipped 1 already listed in {ws}\n")
    code, out = run("commands", "list")
    assert out.splitlines() == ["[no group]", "  *IDN?    Identify", "  CONF:MODE 1",
                                "[Motor]", "  MOTor:SPEed?    Speed in rpm", "  MOTor:STOP"]
    assert run("commands", "list", "speed")[1].splitlines() == ["[Motor]", "  MOTor:SPEed?    Speed in rpm"]
    exported = tmp_path / "out.csv"
    assert run("commands", "export", str(exported))[0] == 0
    assert exported.read_text(encoding="utf-8-sig").splitlines()[:2] == [
        "command,name,group,description", "MOTor:SPEed?,,Motor,Speed in rpm"]
    assert run("commands", "import", str(txt), "--replace")[1].startswith("imported 2 commands")
    bad = tmp_path / "bad.csv"
    bad.write_text("name,group\nx,y\n", encoding="utf-8")
    assert run("commands", "import", str(bad))[0] == 1
    # the console lists them too
    monkeypatch.setattr("sys.stdin", io.StringIO(".commands conf\n.quit\n"))
    address = "%s:%d" % sim.tcp_address
    out = io.StringIO()
    assert main(["-w", str(ws), "--tcp", address, "console"], out=out) == 0
    assert out.getvalue().splitlines()[1:3] == ["[no group]", "  CONF:MODE 1"]
    assert run("commands", "clear") == (0, f"removed 2 commands in {ws}\n")
