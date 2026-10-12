"""Workspace files: saving and loading, display formats and scaling, write safety, and the CLI."""

import io
import re
import tomllib
from pathlib import Path

import pytest

from rdt import ConfigError, Profile, Range, WriteRefused
from rdt.cli import main
from rdt.scaling import Display
from rdt.target import ConnectionConfig, McuConfig, TargetConfig
from rdt.tomlw import Hex, dumps
from rdt.workspace import Tab, Watch, Workspace, canonical
from sim import Simulator, TransportConfig, load_config

from host_helpers import make_target

ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = ROOT / "sim" / "examples" / "firmware"
EXAMPLE_TARGET = ROOT / "examples" / "sim_target.toml"
EXAMPLE_WORKSPACE = ROOT / "examples" / "sim_bench.rdtw"


def one_tab(*watches: Watch) -> list[Tab]:
    return [Tab("Watch", list(watches))]


# --- the TOML writer -------------------------------------------------------------------------

def test_toml_writer_round_trips():
    data = {
        "name": 'quote " back \\ slash\nnew line', "n": 3, "x": 0.5, "big": 1e300, "flag": False,
        "addr": Hex(0x48000000), "list": [1, 2.5, "three"], "skipped": None,
        "table": {"a": 1, "dotted key": "v"},
        "item": [{"id": 0, "sub": [{"start": Hex(16)}]}, {"id": 1}],
    }
    text = dumps(data, "header line")
    assert text.startswith("# header line\n")
    assert "addr = 0x48000000" in text.replace("  ", "")
    back = tomllib.loads(text)
    data.pop("skipped")
    assert back == data


# --- display and scaling ---------------------------------------------------------------------

def test_scaling_and_its_inverse():
    d = Display(k=0.5, offset=10, unit="rpm")
    assert d.to_shown(100) == 60.0
    assert d.to_raw(60.0, is_float=False) == 100
    assert d.text(60.0) == "60 rpm"
    q = Display(q="Q15")
    assert q.fraction_bits == 15 and q.to_shown(-16384) == -0.5
    assert q.to_raw(0.25, is_float=False) == 8192
    assert Display(q="Q3.12").fraction_bits == 12
    assert Display(k=2).to_raw(1.5, is_float=True) == 0.75   # a float variable keeps its fraction
    assert Display().to_shown(7) == 7 and Display().text(True) == "true"


def test_integer_formats():
    assert Display(format="hex").text(-20, 32) == "0xFFFFFFEC"
    assert Display(format="bin", unit="V").text(5, 4) == "0b0101 V"
    assert Display(format="char").text(65, 8) == "'A'"
    assert Display(format="char").text(7, 8) == "0x07"


@pytest.mark.parametrize("kwargs, message", [
    ({"format": "oct"}, "format must be one of"),
    ({"q": "Q1.2.3"}, "must look like Q15"),
    ({"k": 0}, "scale k must not be 0"),
])
def test_bad_display(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        Display(**kwargs)


# --- the watch list --------------------------------------------------------------------------

def test_watch_items_are_canonical_and_checked():
    assert canonical(" main : gains [ 2 ] ") == "main:gains[2]"
    assert Watch("main: motor .speed").expr == "main:motor.speed"
    with pytest.raises(ConfigError, match="poll must be global, manual or a time"):
        Watch("main:x", poll="sometimes")
    with pytest.raises(ConfigError, match="min is above max"):
        Watch("main:x", min=5, max=1)
    with pytest.raises(ConfigError, match="unknown keys colour"):
        Watch.from_dict({"expr": "main:x", "colour": "red"})
    assert Watch("main:x", poll="manual").interval_ms(500) is None
    assert Watch("main:x").interval_ms(0) is None   # global polling off
    assert Watch("main:x", poll=50).interval_ms(500) == 50


def test_guard_for_finds_the_watched_parent():
    ws = Workspace(tabs=one_tab(Watch("main:motor", read_only=True), Watch("main:motor.speed"), Watch("main:gains")))
    assert [w and w.expr for w in ws.guard_for("main:motor.speed")] == ["main:motor.speed", "main:motor"]
    assert [w and w.expr for w in ws.guard_for("main:motor.target")] == [None, "main:motor"]
    assert [w and w.expr for w in ws.guard_for("main:gains[1]")] == [None, "main:gains"]
    assert ws.guard_for("main:motorx") == (None, None)


def test_tabs():
    ws = Workspace()
    assert ws.watches == [] and ws.tabs == []
    ws.add(Watch("main:motor.speed", Display(unit="rpm")))   # the first tab is made when needed
    assert [t.name for t in ws.tabs] == ["Watch"]
    ws.add_tab("Motor")
    with pytest.raises(ConfigError, match="already a tab"):
        ws.add_tab("motor")
    with pytest.raises(ConfigError, match="needs a name"):
        ws.add_tab("  ")
    shared = ws.add(Watch("main:motor.speed"), "motor")   # the item is shared, with its settings
    assert shared is ws.tab("Watch").watches[0] and shared.display.unit == "rpm"
    ws.add(Watch("main:tick_count"), "Motor")
    with pytest.raises(ConfigError, match="already in tab 'Motor'"):
        ws.add(Watch("main: tick_count"), "Motor")
    assert [w.expr for w in ws.watches] == ["main:motor.speed", "main:tick_count"]   # each once
    ws.replace(Watch("main:motor.speed", Display(unit="rad/s")))
    assert all(t.watches[0].display.unit == "rad/s" for t in ws.tabs)
    assert [t.name for t in ws.tabs_with("main:motor.speed")] == ["Watch", "Motor"]
    ws.remove("main:motor.speed", "Watch")
    assert [t.name for t in ws.tabs_with("main:motor.speed")] == ["Motor"]
    with pytest.raises(ConfigError, match="not in tab 'Watch'"):
        ws.remove("main:motor.speed", "Watch")
    ws.rename_tab("motor", "Drive")
    with pytest.raises(ConfigError, match="no tab 'Motor'"):
        ws.tab("Motor")
    ws.remove("main:motor.speed")   # from every tab
    assert ws.remove_tab("Drive").watches[0].expr == "main:tick_count"
    assert [t.name for t in ws.tabs] == ["Watch"] and ws.watches == []


def test_tabs_save_and_load(tmp_path):
    speed = Watch("main:motor.speed", Display(unit="rpm"), poll=100)
    ws = Workspace(tabs=[Tab("Motor", [speed, Watch("main:motor.mode", read_only=True)]),
                         Tab("Empty"), Tab("Overview", [Watch("main:tick_count"), speed])])
    path = ws.save(tmp_path / "tabs.rdtw")
    raw = tomllib.loads(path.read_text())
    assert [t["name"] for t in raw["tab"]] == ["Motor", "Empty", "Overview"]
    assert raw["tab"][2]["watch"][1] == {"expr": "main:motor.speed"}   # settings at the first place only
    back = Workspace.load(path)
    assert [(t.name, [w.expr for w in t.watches]) for t in back.tabs] == \
           [(t.name, [w.expr for w in t.watches]) for t in ws.tabs]
    assert back.tabs[2].watches[1] is back.tabs[0].watches[0] and back.tabs[0].watches[0] == speed


def test_an_old_workspace_loads(tmp_path):
    """A file from before tabs: its [[watch]] list is the tab "Watch", and buttons are dropped."""
    path = tmp_path / "old.rdtw"
    path.write_text('[[watch]]\nexpr = "main:a"\nunit = "V"\n\n'
                    '[[button]]\nname = "Who"\ncommand = "*IDN?"\n')
    ws = Workspace.load(path)
    assert [(t.name, [w.expr for w in t.watches]) for t in ws.tabs] == [("Watch", ["main:a"])]
    assert ws.watches[0].display.unit == "V"
    assert "buttons are no longer supported" in ws.warnings[0]
    ws.save()
    text = path.read_text()
    assert "button" not in text and "[[tab]]" in text and "[[tab.watch]]" in text
    assert Workspace.load(path).warnings == []


# --- files -----------------------------------------------------------------------------------

def test_save_and_load(tmp_path):
    profile = Profile(name="bench", timeout_ms=800, size_query=None,
                      ranges=[Range("SRAM2", 0x10000000, 0x8000, mcu="main")])
    cfg = TargetConfig(ConnectionConfig("serial", "COM5", 921600), profile,
                       [McuConfig(0, "main", FIRMWARE / "main.elf",
                                  ranges=[Range("GPIOA", 0x48000000, 0x400, writable=False)]),
                        McuConfig(1, "motorctl", FIRMWARE / "motorctl.elf", timeout_ms=1500)])
    ws = Workspace(cfg, one_tab(Watch("main:motor.speed", Display(k=0.1, unit="rpm"), poll=100, min=0, max=3000,
                                      confirm=True),
                                Watch("main:tick_count", Display(format="hex"), poll="manual", read_only=True)),
                   poll_ms=250,
                   chart={"window_s": 10, "axes": [{"items": ["main:motor.speed"]}]})
    (tmp_path / "sub").mkdir()
    path = ws.save(tmp_path / "sub" / "bench.rdtw")

    text = path.read_text()
    assert "sim/examples/firmware/main.elf" in text and "\\" not in text   # relative, with / on every OS
    assert re.search(r"^start +=  ?0x48000000$", text, re.M)

    back = Workspace.load(path)
    assert back.config.connection == cfg.connection
    assert back.config.profile.name == "bench" and back.config.profile.timeout_ms == 800
    assert not back.config.profile.size_query
    assert back.config.profile.ranges == profile.ranges
    assert [(m.id, m.name, m.elf.resolve(), m.timeout_ms, m.ranges) for m in back.config.mcus] == \
           [(m.id, m.name, m.elf.resolve(), m.timeout_ms, m.ranges) for m in cfg.mcus]
    assert back.tabs == ws.tabs
    assert (back.poll_ms, back.chart) == (250, ws.chart)
    # Saving again changes nothing.
    back.save()
    assert path.read_text() == text


def test_a_target_file_is_a_workspace():
    ws = Workspace.load(EXAMPLE_TARGET)
    assert [m.name for m in ws.config.mcus] == ["main", "motorctl"]
    assert ws.watches == [] and ws.poll_ms == 500
    assert ws.config.profile_path.name == "default_profile.toml"


def test_the_example_workspace_loads():
    ws = Workspace.load(EXAMPLE_WORKSPACE)
    assert [t.name for t in ws.tabs] == ["Motor", "System"]
    assert ws.tab("System").watch("main:motor.mode") is ws.tab("Motor").watch("main:motor.mode")
    assert all(m.elf.exists() for m in ws.config.mcus)


@pytest.mark.parametrize("text, message", [
    ('[[watch]]\nexpr = "main:a"\n[[watch]]\nexpr = "main: a"\n', "in tab 'Watch' twice"),
    ('[[tab]]\nname = "A"\n[[tab]]\nname = "a"\n', "two tabs named 'a'"),
    ('[[tab]]\nwatch = []\n', "a tab needs a name"),
    ('[[tab]]\nname = "A"\ncolor = "red"\n', "unknown keys color"),
    ('[[tab]]\nname = "A"\n[[tab.watch]]\nexpr = "main:a"\nunit = "V"\n'
     '[[tab]]\nname = "B"\n[[tab.watch]]\nexpr = "main:a"\nunit = "mV"\n', "different settings in two tabs"),
    ('[[watch]]\nformat = "hex"\n', "needs an expr"),
    ('poll_ms = -5\n', "poll_ms must be"),
    ('chart = 3\n', r"\[chart\] must be a table"),
])
def test_bad_workspaces(tmp_path, text, message):
    path = tmp_path / "bad.rdtw"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        Workspace.load(path)


# --- reading and writing through the workspace -----------------------------------------------

@pytest.fixture
def sim():
    cfg = load_config(ROOT / "sim" / "examples" / "two_mcus.toml")
    cfg.device.mcus[0].signals.clear()   # stable values
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


@pytest.fixture
def target(sim):
    with make_target("tcp", sim.tcp_address) as t:
        yield t


def test_read_with_scaling_and_formats(target):
    ws = Workspace(tabs=one_tab(
        Watch("main:motor.current_ma", Display(k=0.001, unit="A")),
        Watch("main:gains", Display(format="hex")),
        Watch("main:motor.target", Display(k=0.5, unit="rpm")),
        Watch("main:tick_count", Display(q="Q31")),
    ))
    _, shown = ws.read(target, "main:motor.current_ma")
    assert [(s.text, s.raw) for s in shown] == [("-0.12 A", -120)]
    _, shown = ws.read(target, "main:gains")
    assert [s.text for s in shown] == ["0x0000000A", "0xFFFFFFEC", "0x0000001E", "0xFFFFFFD8"]
    _, shown = ws.read(target, "main:motor.target")
    assert shown[0].text == "1500 rpm" and shown[0].value == 1500.0
    _, shown = ws.read(target, "main:tick_count")
    assert shown[0].value == pytest.approx(0x12345678 / 2**31)
    _, shown = ws.read(target, "main:motor")   # not in the list: shown as rdt read shows it
    assert shown[-1].text == "MODE_RUN (1)"
    _, shown = ws.read(target, "main:motor", compact=True)
    assert shown[-1].text == "MODE_RUN" and shown[2].text == "-120"
    with pytest.raises(ConfigError, match="scaling needs a single value"):
        ws.read(target, Watch("main:motor", Display(k=2)))


def test_write_applies_the_inverse_scaling(target):
    ws = Workspace(tabs=one_tab(Watch("main:gains[2]", Display(k=0.5, offset=1)),
                                Watch("main:motor.current_ma", Display(q="Q3.12")),
                            Watch("main:motor.target", Display(k=2, unit="rpm"))))
    assert ws.write(target, "main:gains[2]", "22")[1] == 42
    assert target.read_value("main:gains[2]") == 42
    assert ws.write(target, "main:motor.current_ma", "-0.5")[1] == -2048
    assert target.read_value("main:motor.current_ma") == -2048
    ws.write(target, "main:motor.target", "1001")   # a float variable keeps the fraction
    assert target.read_value("main:motor.target") == 500.5
    with pytest.raises(ConfigError, match="does not fit in int16_t"):
        ws.write(target, "main:motor.current_ma", "9")   # 9 * 4096 is too big for 16 bits


def test_write_safety(target):
    ws = Workspace(tabs=one_tab(
        Watch("main:motor", read_only=True),
        Watch("main:gains", confirm=True),
        Watch("main:gains[0]", Display(unit="%"), min=-10, max=100),
        Watch("main:tick_count", confirm=True),
    ))
    with pytest.raises(WriteRefused, match="main:motor.target is read-only in the workspace .it is part of main:motor"):
        ws.write(target, "main:motor.target", "1")
    with pytest.raises(WriteRefused, match="101 is above its maximum 100"):
        ws.write(target, "main:gains[0]", "101", confirm=lambda q: True)
    with pytest.raises(WriteRefused, match="-11 is below its minimum -10"):
        ws.write(target, "main:gains[0]", "-11", confirm=lambda q: True)
    asked = []
    with pytest.raises(WriteRefused, match="write not confirmed"):
        ws.write(target, "main:gains[0]", "50", confirm=lambda q: asked.append(q) or False)
    assert asked == ["Write 50 % to main:gains[0]?"]
    with pytest.raises(WriteRefused, match="write not confirmed"):
        ws.write(target, "main:tick_count", "1")   # nobody to ask: refused
    ws.write(target, "main:gains[0]", "50", confirm=lambda q: True)
    assert target.read_value("main:gains[0]") == 50
    assert target.read_value("main:motor.target") == 3000.0   # untouched


# --- the CLI ---------------------------------------------------------------------------------

def rdt(sim, ws_path, *argv):
    out = io.StringIO()
    code = main(["-w", str(ws_path), "--tcp", "%s:%d" % sim.tcp_address, *argv], out=out)
    return code, out.getvalue()


def test_cli_builds_a_workspace_without_a_device(tmp_path, capsys):
    path = tmp_path / "bench.rdtw"
    off = ["--tcp", "127.0.0.1:1"]   # nothing listens: these commands never connect
    assert main(["-t", str(EXAMPLE_TARGET), *off, "init", str(path)], out=io.StringIO()) == 0
    assert main(["-t", str(EXAMPLE_TARGET), *off, "init", str(path)]) == 1
    assert "exists; add --force" in capsys.readouterr().err
    out = io.StringIO()
    for argv in (["add", "main:motor.target", "--scale", "0.5", "--unit", "rpm", "--max", "2000"],
                 ["add", "main:tick_count", "--format", "hex", "--poll", "manual"],
                 ["add", "main:motor.target", "--confirm"],   # changes the item, keeps the rest
                 ["add", "main:gains"], ["remove", "main:gains"]):
        assert main(["-w", str(path), *off, *argv], out=out) == 0
    assert main(["-w", str(path), *off, "add", "main:nope"]) == 1
    assert "no variable 'nope'" in capsys.readouterr().err
    ws = Workspace.load(path)
    assert [w.to_dict() for w in ws.watches] == [
        {"expr": "main:motor.target", "scale": 0.5, "unit": "rpm", "max": 2000.0, "confirm": True},
        {"expr": "main:tick_count", "poll": "manual", "format": "hex"},
    ]
    out = io.StringIO()
    assert main(["-w", str(path), *off, "list"], out=out) == 0
    assert "main:tick_count    poll=manual format=hex" in out.getvalue()
    assert "tab 'Watch': 2 items" in out.getvalue()

    for argv in (["add", "main:motor.speed", "--tab", "Motor", "--unit", "rpm"],   # makes the tab
                 ["add", "main:tick_count", "--tab", "motor"],   # shared with tab Watch
                 ["add", "main:tick_count", "--format", "dec"],   # changed where it is, in both tabs
                 ["tab", "add", "Spare"], ["tab", "rename", "spare", "Later"], ["tab", "remove", "Later"],
                 ["remove", "main:tick_count", "--tab", "Watch"]):
        assert main(["-w", str(path), *off, *argv], out=out) == 0, argv
    assert main(["-w", str(path), *off, "tab", "remove", "nope"]) == 1
    assert "no tab 'nope'" in capsys.readouterr().err
    ws = Workspace.load(path)
    assert [(t.name, [w.expr for w in t.watches]) for t in ws.tabs] == [
        ("Watch", ["main:motor.target"]), ("Motor", ["main:motor.speed", "main:tick_count"])]
    assert ws.watch("main:tick_count").display.format == "dec"


def test_cli_reads_and_writes_the_watch_list(sim, tmp_path, capsys):
    path = tmp_path / "bench.rdtw"
    ws = Workspace.load(EXAMPLE_TARGET)
    ws.tabs = [Tab("Motor", [Watch("main:motor.target", Display(k=0.5, unit="rpm"), max=2000, confirm=True)]),
               Tab("System", [Watch("main:tick_count", Display(format="hex"))])]
    ws.save(path)
    code, out = rdt(sim, path, "read")
    assert (code, out.splitlines()) == (0, ["main:motor.target = 1500 rpm", "main:tick_count = 0x12345678"])
    assert rdt(sim, path, "write", "main:motor.target", "1000")[0] == 1
    assert "write not confirmed" in capsys.readouterr().err
    assert rdt(sim, path, "write", "main:motor.target", "1000", "-y") == (
        0, "main:motor.target = 1000 rpm (confirmed by read-back)\n")
    assert rdt(sim, path, "write", "main:motor.target", "2500", "-y")[0] == 1
    assert "above its maximum 2000" in capsys.readouterr().err
    code, out = rdt(sim, path, "watch", "-n", "2", "-i", "20")
    lines = out.splitlines()
    assert lines[0].split() == ["time", "main:motor.target", "main:tick_count"]
    assert lines[1].split()[1:] == ["1000", "rpm", "0x12345678"]
    code, out = rdt(sim, path, "read", "--tab", "system")
    assert (code, out) == (0, "main:tick_count = 0x12345678\n")
    assert rdt(sim, path, "send", "SIM:MCU:SIL 1,ON")[0] == 0
    assert "MCU motorctl is not responding" in rdt(sim, path, "read", "motorctl:pwm_duty")[1]


def test_saved_commands_round_trip(tmp_path):
    from rdt.console import SavedCommand
    ws = Workspace()
    ws.commands = [SavedCommand("MOT:SPE?", "Speed", "Motor", "rpm"), SavedCommand("*IDN?")]
    path = ws.save(tmp_path / "w.rdtw")
    assert Workspace.load(path).commands == ws.commands
    assert '[[command]]\ncommand = "*IDN?"\n' in path.read_text(encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8") + '\n[[command]]\nname = "no command"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[\[command\]\]"):
        Workspace.load(path)
