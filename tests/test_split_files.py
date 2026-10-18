"""The workspace split into files: watch list, known commands and interface settings, each with
import and export, from the workspace API and the CLI."""

import io
import tomllib
from pathlib import Path

import pytest

from rdt import ConfigError
from rdt.cli import main
from rdt.console import SavedCommand
from rdt.profile import Range
from rdt.scaling import Display
from rdt.target import McuConfig, TargetConfig
from rdt.ui_settings import UiSettings, ui_path
from rdt.workspace import Tab, Watch, Workspace

ROOT = Path(__file__).resolve().parent.parent

OLD = '''poll_ms = 250

[connection]
transport = "tcp"
address = "127.0.0.1:5025"

[[mcu]]
id = 0
name = "main"

[[tab]]
name = "Motor"

  [[tab.watch]]
  expr = "main:motor.speed"
  unit = "rpm"

[chart]
window_s = 10

[[command]]
command = "MOT:SPE?"
name = "Speed"
'''


def bench() -> Workspace:
    return Workspace(TargetConfig(mcus=[McuConfig(0, "main")]),
                     [Tab("Motor", [Watch("main:motor.speed", Display(unit="rpm"))]), Tab("System")],
                     chart={"window_s": 10},
                     commands=[SavedCommand("MOT:SPE?", "Speed")])


def test_save_writes_three_files(tmp_path):
    path = bench().save(tmp_path / "bench.rdtw")
    raw = tomllib.loads(path.read_text())
    assert raw["watch_file"] == "bench.watch.toml" and raw["commands_file"] == "bench.commands.toml"
    assert not {"tab", "watch", "chart", "command"} & set(raw)
    watch = tomllib.loads((tmp_path / "bench.watch.toml").read_text())
    assert [t["name"] for t in watch["tab"]] == ["Motor", "System"] and watch["chart"] == {"window_s": 10}
    assert tomllib.loads((tmp_path / "bench.commands.toml").read_text()) == \
        {"command": [{"command": "MOT:SPE?", "name": "Speed"}]}
    back = Workspace.load(path)
    assert [t.name for t in back.tabs] == ["Motor", "System"] and back.watches[0].display.unit == "rpm"
    assert back.chart == {"window_s": 10} and back.commands == [SavedCommand("MOT:SPE?", "Speed")]
    assert back.warnings == []


def test_nothing_to_keep_makes_no_files(tmp_path):
    path = Workspace(TargetConfig(mcus=[McuConfig(0, "main")])).save(tmp_path / "empty.rdtw")
    # Only the profile file is always written (rani, 2026-10-15).
    assert "_file" not in path.read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["empty.profile.toml", "empty.rdtw"]


def test_an_old_single_file_workspace_is_split_on_save(tmp_path):
    path = tmp_path / "old.rdtw"
    path.write_text(OLD)
    ws = Workspace.load(path)
    assert ws.watch_path is None and ws.tabs[0].watches[0].expr == "main:motor.speed" and ws.commands
    ws.save()
    text = path.read_text()
    assert "[[tab" not in text and "[chart]" not in text and "[[command]]" not in text
    assert (tmp_path / "old.watch.toml").is_file() and (tmp_path / "old.commands.toml").is_file()
    back = Workspace.load(path)
    assert back.tabs[0].watches[0].display.unit == "rpm" and back.chart == {"window_s": 10}
    assert back.commands == [SavedCommand("MOT:SPE?", "Speed")] and back.poll_ms == 250


def test_save_as_takes_its_files_along(tmp_path):
    ws = bench()
    ws.save(tmp_path / "a.rdtw")
    shared = tmp_path / "shared.toml"
    ws.commands_path = shared                  # loaded from a file under another name
    ws.save(tmp_path / "b.rdtw")
    raw = tomllib.loads((tmp_path / "b.rdtw").read_text())
    assert raw["watch_file"] == "b.watch.toml"   # always beside the .rdtw, named after it
    assert raw["commands_file"] == "b.commands.toml"
    assert not shared.exists()
    assert (tmp_path / "a.watch.toml").is_file() and (tmp_path / "b.watch.toml").is_file()


def test_files_in_another_folder_come_beside_the_workspace(tmp_path):
    """The .rdtw and its files sit in one folder (rani, 2026-10-14): a commands file named from
    elsewhere still loads, and saving writes the commands beside the .rdtw; the old file is left."""
    (tmp_path / "lists").mkdir()
    (tmp_path / "lists" / "cmds.csv").write_text("*IDN?\n")
    (tmp_path / "w.rdtw").write_text('commands_file = "lists/cmds.csv"\n')
    ws = Workspace.load(tmp_path / "w.rdtw")
    assert ws.commands == [SavedCommand("*IDN?")]
    ws.commands.append(SavedCommand("MOT:SPE?"))
    ws.save()
    assert (tmp_path / "lists" / "cmds.csv").read_text() == "*IDN?\n"
    assert 'commands_file = "w.commands.toml"' in (tmp_path / "w.rdtw").read_text()
    assert Workspace.load(tmp_path / "w.rdtw").commands == [SavedCommand("*IDN?"), SavedCommand("MOT:SPE?")]


def test_workspace_folder_is_beside_ui_toml(tmp_path, monkeypatch):
    from rdt.ui_settings import workspace_dir
    from rdt.workspace import default_target
    monkeypatch.setenv("RDT_UI_FILE", str(tmp_path / "rdt" / "ui.toml"))
    monkeypatch.chdir(tmp_path)
    assert workspace_dir() == tmp_path / "rdt" and default_target() is None
    (tmp_path / "rdt").mkdir()
    (tmp_path / "rdt" / "rdt.toml").write_text("")
    assert default_target() == tmp_path / "rdt" / "rdt.toml"
    (tmp_path / "rdt.toml").write_text("")      # the current folder's comes first
    assert default_target() == Path("rdt.toml")


def test_commands_csv_from_before_becomes_toml(tmp_path):
    """A workspace saved by the first split keeps its commands in bench.commands.csv: it still
    loads, and saving moves them to bench.commands.toml (rani, 2026-10-14)."""
    (tmp_path / "bench.commands.csv").write_text("command,name,group,description\nMOT:SPE?,Speed,Motor,rpm\n")
    (tmp_path / "bench.rdtw").write_text('commands_file = "bench.commands.csv"\n')
    ws = Workspace.load(tmp_path / "bench.rdtw")
    assert ws.commands == [SavedCommand("MOT:SPE?", "Speed", "Motor", "rpm")] and ws.warnings == []
    ws.save()
    assert tomllib.loads((tmp_path / "bench.rdtw").read_text())["commands_file"] == "bench.commands.toml"
    assert tomllib.loads((tmp_path / "bench.commands.toml").read_text()) == {"command": [
        {"command": "MOT:SPE?", "name": "Speed", "group": "Motor", "description": "rpm"}]}
    assert Workspace.load(tmp_path / "bench.rdtw").commands == ws.commands
    ws.save(tmp_path / "other.rdtw")            # and it follows a new name, like the watch list
    assert (tmp_path / "other.commands.toml").is_file()


def test_bad_commands_file(tmp_path):
    (tmp_path / "c.toml").write_text('[[command]]\nname = "no command"\n')
    (tmp_path / "w.rdtw").write_text('commands_file = "c.toml"\n')
    with pytest.raises(ConfigError, match=r"commands file .*c.toml: every \[\[command\]\]"):
        Workspace.load(tmp_path / "w.rdtw")
    (tmp_path / "c.toml").write_text('[[tab]]\nname = "A"\n')
    with pytest.raises(ConfigError, match="unknown keys tab"):
        Workspace.load(tmp_path / "w.rdtw")


def test_a_missing_file_is_a_warning(tmp_path):
    (tmp_path / "w.rdtw").write_text('watch_file = "gone.watch.toml"\ncommands_file = "gone.csv"\n')
    ws = Workspace.load(tmp_path / "w.rdtw")
    assert ws.tabs == [] and ws.commands == []
    assert any("gone.watch.toml is missing" in w for w in ws.warnings)
    assert any("gone.csv is missing" in w for w in ws.warnings)


def test_both_places_is_an_error(tmp_path):
    (tmp_path / "w.rdtw").write_text('watch_file = "w.watch.toml"\n\n[[tab]]\nname = "A"\n')
    with pytest.raises(ConfigError, match="keep the watch tabs in one place"):
        Workspace.load(tmp_path / "w.rdtw")
    (tmp_path / "w.rdtw").write_text('commands_file = "c.csv"\n\n[[command]]\ncommand = "*IDN?"\n')
    with pytest.raises(ConfigError, match="keep the commands in one place"):
        Workspace.load(tmp_path / "w.rdtw")


def test_watch_list_import_and_export(tmp_path):
    src = bench()
    src.tabs[0].watches.append(Watch("main:motor.mode", read_only=True))
    out = src.export_watches(tmp_path / "motor.watch.toml")
    assert "connection" not in out.read_text()

    ws = Workspace(tabs=[Tab("Motor", [Watch("main:motor.speed", Display(unit="rad/s"))]), Tab("Mine")],
                   chart={"window_s": 3})
    assert ws.import_watches(out) == (1, 1)        # speed is already in Motor: skipped
    assert [t.name for t in ws.tabs] == ["Motor", "Mine", "System"]
    assert [w.expr for w in ws.tab("Motor").watches] == ["main:motor.speed", "main:motor.mode"]
    assert ws.watch("main:motor.speed").display.unit == "rad/s"   # kept its settings here
    assert ws.chart == {"window_s": 3}             # adding keeps the chart

    assert ws.import_watches(out, replace=True) == (2, 0)
    assert [t.name for t in ws.tabs] == ["Motor", "System"] and ws.chart == {"window_s": 10}

    old = tmp_path / "old.rdtw"                     # another workspace works too, old or new
    old.write_text(OLD)
    ws = Workspace()
    assert ws.import_watches(old) == (1, 0) and ws.tabs[0].name == "Motor"

    bad = tmp_path / "bad.toml"
    bad.write_text('[connection]\ntransport = "tcp"\n')
    with pytest.raises(ConfigError, match="unknown keys connection"):
        ws.import_watches(bad)


def test_commands_import_and_export(tmp_path):
    ws = bench()
    assert "[[command]]" in ws.export_commands(tmp_path / "c.toml").read_text()
    assert Workspace().import_commands(tmp_path / "c.toml") == (1, 0)
    out = ws.export_commands(tmp_path / "c.csv")
    other = Workspace(commands=[SavedCommand("*IDN?")])
    assert other.import_commands(out) == (1, 0)
    assert [c.command for c in other.commands] == ["*IDN?", "MOT:SPE?"]
    assert other.import_commands(out, replace=True) == (1, 0) and len(other.commands) == 1


# --- interface settings ---------------------------------------------------------------------

def test_ui_settings_file(tmp_path):
    assert ui_path() == tmp_path / "ui.toml"       # next to the console history
    ui = UiSettings.load()
    assert not ui.exists() and ui.value("console/font_size", 9) == 9
    ui.set_value("console/font_size", 12)
    ui.set_value("session/last_workspace", "C:/bench.rdtw")
    other = UiSettings.load()                       # a second window
    other.set_value("memory/kind", "float")
    ui.set_value("console/wrap", True)              # re-reads first: keeps the other window's change
    back = UiSettings.load()
    assert back.values == {"console/font_size": 12, "console/wrap": True, "memory/kind": "float",
                           "session/last_workspace": "C:/bench.rdtw"}
    text = ui_path().read_text()
    assert "[console]" in text and "font_size = 12" in text

    exported = back.export(tmp_path / "mine.toml")
    assert "last_workspace" not in exported.read_text()
    fresh = UiSettings(tmp_path / "fresh.toml", {"console/font_size": 20, "session/last_workspace": "x.rdtw"})
    assert fresh.import_file(exported) == ["console/font_size", "console/wrap", "memory/kind"]
    assert fresh.values["console/font_size"] == 12 and fresh.values["session/last_workspace"] == "x.rdtw"

    bad = tmp_path / "bad.toml"
    bad.write_text("[console]\nfont_size = \"big\"\n")
    with pytest.raises(ConfigError, match="font_size must be a whole number"):
        fresh.import_file(bad)
    bad.write_text("[session]\nlast_workspace = \"x\"\n")
    with pytest.raises(ConfigError, match="not an interface setting"):
        fresh.import_file(bad)
    ui_path().write_text("not toml [")
    assert UiSettings.load().values == {}           # a broken file: the defaults


def test_ui_setting_set_back_after_another_window_changed_it(tmp_path):
    """L15: setting a key back to the value this window last saw is still saved when another
    window changed it in between."""
    ui = UiSettings.load()
    ui.set_value("console/font_size", 12)
    other = UiSettings.load()                       # a second window
    other.set_value("console/font_size", 14)
    ui.set_value("console/font_size", 12)           # this window still holds 12
    assert UiSettings.load().value("console/font_size") == 12


# --- CLI ------------------------------------------------------------------------------------

def run(*argv):
    out = io.StringIO()
    return main(["--tcp", "127.0.0.1:1", *argv], out=out), out.getvalue()


def test_cli_watchlist_and_ui(tmp_path, capsys):
    a, b = tmp_path / "a.rdtw", tmp_path / "b.rdtw"
    bench().save(a)
    Workspace(TargetConfig(mcus=[McuConfig(0, "main")])).save(b)
    exported = tmp_path / "x.watch.toml"
    assert run("-w", str(a), "watchlist", "export", str(exported)) == \
        (0, f"wrote 2 tabs (1 watched items) to {exported}\n")
    assert run("-w", str(b), "watchlist", "import", str(exported)) == (0, f"imported 1 items; {b} now has 2 tabs\n")
    assert (tmp_path / "b.watch.toml").is_file()
    assert run("-w", str(b), "watchlist", "import", str(exported))[1].startswith(
        "imported 0 items, skipped 1 already in their tab")
    assert run("-w", str(b), "watchlist", "import", str(exported), "--replace")[0] == 0
    assert run("-w", str(b), "watchlist", "import", str(tmp_path / "nope.toml"))[0] == 1
    assert "cannot read watch list" in capsys.readouterr().err

    code, out = run("ui", "show")
    assert code == 0 and "not made yet" in out and "console/font_size" in out
    src = tmp_path / "src.toml"
    src.write_text("[console]\nfont_size = 14\nwrap = true\n")
    assert run("ui", "import", str(src))[0] == 0
    assert "console/font_size  14" in run("ui", "show")[1]
    assert run("ui", "export", str(tmp_path / "back.toml"))[0] == 0
    assert tomllib.loads((tmp_path / "back.toml").read_text()) == {"console": {"font_size": 14, "wrap": True}}


MEMORY = '''[[ranges]]
name = "GPIOA"
start = 0x40020000
size = 0x400

[[ranges]]
name = "DTCM"
start = 0x20000000
size = 0x20000
kind = "memory"
mcu = "main"
'''


def test_memory_map_has_its_own_file(tmp_path):
    ws = bench()
    ws.config.mcus[0].ranges = [Range("SRAM", 0x24000000, 0x80000, kind="memory")]
    ws.config.profile.ranges = [Range("GPIOA", 0x40020000, 0x400)]
    ws.save(tmp_path / "bench.rdtw")
    raw = tomllib.loads((tmp_path / "bench.rdtw").read_text())
    assert raw["memory_file"] == "bench.memory.toml" and "ranges" not in raw and "ranges" not in raw["mcu"][0]
    assert "ranges" not in tomllib.loads((tmp_path / "bench.profile.toml").read_text())
    mem = tomllib.loads((tmp_path / "bench.memory.toml").read_text())
    assert [r["name"] for r in mem["ranges"]] == ["GPIOA", "SRAM"] and mem["ranges"][1]["mcu"] == "main"
    back = Workspace.load(tmp_path / "bench.rdtw")
    assert sorted(r.name for r in back.memory_ranges()) == ["GPIOA", "SRAM"]
    back.save()
    assert tomllib.loads((tmp_path / "bench.rdtw").read_text())["memory_file"] == "bench.memory.toml"


def test_no_ranges_no_memory_file(tmp_path):
    bench().save(tmp_path / "bench.rdtw")
    assert not (tmp_path / "bench.memory.toml").exists()
    assert "memory_file" not in tomllib.loads((tmp_path / "bench.rdtw").read_text())


def test_old_inline_ranges_move_to_the_memory_file(tmp_path):
    (tmp_path / "old.rdtw").write_text(
        '[connection]\ntransport = "tcp"\naddress = "127.0.0.1:5025"\n\n[[mcu]]\nid = 0\nname = "main"\n\n'
        '  [[mcu.ranges]]\n  name = "GPIOA"\n  start = 0x40020000\n  size = 0x400\n')
    ws = Workspace.load(tmp_path / "old.rdtw")
    assert [r.name for r in ws.config.mcus[0].ranges] == ["GPIOA"]
    ws.save()
    assert "ranges" not in tomllib.loads((tmp_path / "old.rdtw").read_text())["mcu"][0]
    assert (tmp_path / "old.memory.toml").is_file()
    assert [r.name for r in Workspace.load(tmp_path / "old.rdtw").memory_ranges()] == ["GPIOA"]


def test_load_memory_map(tmp_path):
    (tmp_path / "m.memory.toml").write_text(MEMORY)
    ws = bench()
    assert ws.import_memory(tmp_path / "m.memory.toml") == (2, 0)
    assert ws.import_memory(tmp_path / "m.memory.toml") == (0, 2)        # again: all skipped
    assert ws.import_memory(tmp_path / "m.memory.toml", replace_all=True) == (2, 0)
    t = Workspace.load(ws.save(tmp_path / "w.rdtw"))
    assert {r.name for r in t.memory_ranges()} == {"GPIOA", "DTCM"}


def test_load_memory_map_refuses_overlap(tmp_path):
    (tmp_path / "bad.toml").write_text('[[ranges]]\nname = "A"\nstart = 0x1000\nsize = 0x100\n\n'
                                       '[[ranges]]\nname = "B"\nstart = 0x1080\nsize = 0x100\n')
    ws = bench()
    with pytest.raises(ConfigError):
        ws.import_memory(tmp_path / "bad.toml")
    assert ws.memory_ranges() == []


def test_memory_map_from_a_workspace_and_the_cli(tmp_path):
    src = bench()
    src.config.mcus[0].ranges = [Range("SRAM", 0x24000000, 0x80000, kind="memory")]
    src.save(tmp_path / "src.rdtw")
    bench().save(tmp_path / "dst.rdtw")
    out = io.StringIO()
    assert main(["-w", str(tmp_path / "dst.rdtw"), "memory", "import", str(tmp_path / "src.rdtw")], out) == 0
    assert "imported 1 ranges" in out.getvalue()
    exported = tmp_path / "x.memory.toml"
    assert main(["-w", str(tmp_path / "dst.rdtw"), "memory", "export", str(exported)], io.StringIO()) == 0
    assert tomllib.loads(exported.read_text())["ranges"][0]["name"] == "SRAM"
