"""GUI tests: the main window against the simulated device, run offscreen with pytest-qt."""

import os
import re
import shutil
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QInputDialog, QMessageBox  # noqa: E402

from rdt.gui.app import MainWindow  # noqa: E402
from rdt.gui.panels import McuDialog, ProfileDialog  # noqa: E402
from rdt.gui.variables import COL_VALUE, WatchDialog  # noqa: E402
from rdt.symbols import load_symbols  # noqa: E402
from rdt.workspace import Workspace  # noqa: E402
from sim import Simulator, TransportConfig, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sim():
    cfg = load_config(ROOT / "sim" / "examples" / "two_mcus.toml")
    cfg.device.mcus[0].signals.clear()   # stable values
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


@pytest.fixture
def bench(tmp_path, sim):
    """The example workspace, copied next to its ELFs and profile, pointed at the simulator."""
    ws = Workspace.load(ROOT / "examples" / "sim_bench.rdtw")
    ws.config.connection.address = "%s:%d" % tuple(sim.tcp_address)
    ws.config.profile.timeout_ms = 300
    path = tmp_path / "bench.rdtw"
    ws.save(path)
    return path


@pytest.fixture
def win(qtbot, bench):
    w = MainWindow()
    qtbot.addWidget(w, before_close_func=lambda w: setattr(w, "dirty", False))   # no save prompt
    assert w.open_path(bench)
    return w


def connect(qtbot, win):
    win.connect()
    qtbot.waitUntil(lambda: win.device.target is not None, timeout=5000)


def value(win, path):
    for _, st in win.watch_tabs.states():
        node = st.nodes.get(path)
        if node is not None:
            return node.text(COL_VALUE)
    return None


def wait_value(qtbot, win, path, check, timeout=5000):
    qtbot.waitUntil(lambda: bool(v := value(win, path)) and check(v), timeout=timeout)


def test_connect_and_poll(qtbot, win):
    assert win.windowTitle() == "bench.rdtw - RDT"
    assert win.bar.port.value() == int(win.ws.config.connection.address.rsplit(":", 1)[1])
    connect(qtbot, win)
    assert win.bar.state.text() == "Connected \u00b7 " + win.ws.config.connection.address
    wait_value(qtbot, win, "main:motor.current_ma", lambda v: v.endswith(" A"))
    wait_value(qtbot, win, "main:motor.mode", lambda v: "MODE_" in v)
    wait_value(qtbot, win, "main:tick_count", lambda v: v.startswith("0x"))
    # a manual item is read once on connect, then only by hand
    wait_value(qtbot, win, "motorctl:temperature", lambda v: v.endswith("degC"))
    st = win.watch_tree.states["main:motor.speed"]
    qtbot.waitUntil(lambda: st.item.text(4).endswith("/s"), timeout=5000)


def test_struct_expands_and_collapsed_struct_is_not_read(qtbot, win, monkeypatch):
    connect(qtbot, win)
    tree = win.watch_tree
    tree.add_watch("main:motor")
    st = tree.states["main:motor"]
    qtbot.waitUntil(lambda: st.aggregate is not None, timeout=5000)
    assert st.aggregate
    assert {"main:motor.speed", "main:motor.mode", "main:motor.enabled"} <= set(st.nodes)
    assert st.item.text(COL_VALUE) == "{...}"
    # collapsed: the poll leaves it alone
    reads = []
    real = win.ws.read_many
    monkeypatch.setattr(win.ws, "read_many",
                        lambda t, items, *a, **kw: (reads.extend(w.expr for w in items), real(t, items, *a, **kw))[1])
    qtbot.wait(700)
    assert "main:motor" not in reads
    assert "main:motor.speed" in reads
    st.item.setExpanded(True)
    qtbot.waitUntil(lambda: "main:motor" in reads, timeout=2000)


def test_write_inline_with_limits_and_confirm(qtbot, win, monkeypatch):
    connect(qtbot, win)
    tree = win.watch_tree
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    tree.write("main:motor.target", "1500")
    wait_value(qtbot, win, "main:motor.target", lambda v: v.startswith("1500"))
    assert asked == ["Write 1500 rpm to main:motor.target?"]
    assert "confirmed" in win.message.text()

    tree.write("main:motor.target", "5000")     # above max
    qtbot.waitUntil(lambda: "above its maximum" in win.message.text(), timeout=3000)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    tree.write("main:motor.target", "100")
    assert "cancelled" in win.message.text()

    tree.write("main:motor.mode", "MODE_RUN")   # read-only item
    assert "read-only" in win.message.text()

    tree.write("main:motor.current_ma", "1.25")  # scaled: 1.25 A is raw 1250
    wait_value(qtbot, win, "main:motor.current_ma", lambda v: v == "1.25 A")


def test_inline_edit_writes(qtbot, win):
    connect(qtbot, win)
    tree = win.watch_tree
    tree.add_watch("main:gains")
    wait_value(qtbot, win, "main:gains[1]", lambda v: True)
    tree.states["main:gains"].item.setExpanded(True)
    node = tree.states["main:gains"].nodes["main:gains[1]"]
    tree._double_clicked(node, COL_VALUE)
    assert node.flags() & Qt.ItemIsEditable
    node.setText(COL_VALUE, "-42")
    wait_value(qtbot, win, "main:gains[1]", lambda v: v.startswith("-42"))


def test_symbol_browser_adds_and_save_round_trips(qtbot, win, tmp_path):
    sb = win.symbols
    sb.mcu.setCurrentText("main")
    sb.search.setText("rx*")
    assert sb.list.topLevelItemCount() == 1
    sb.list.topLevelItem(0).setSelected(True)
    sb.add_button.click()
    assert "main:rx_buffer" in win.watch_tree.states
    assert win.dirty and win.windowTitle().endswith("* - RDT")
    win.watch_tree.set_format("main:motor.speed", "hex")
    out = tmp_path / "saved.rdtw"
    assert win._save_to(out)
    ws = Workspace.load(out)
    assert ws.watch("main:rx_buffer") is not None
    assert ws.watch("main:motor.speed").display.format == "hex"
    assert ws.watch("main:motor.target").confirm
    assert not win.dirty


def test_console_and_memory(qtbot, win):
    connect(qtbot, win)
    con = win.console
    con.input.setText("*IDN?")
    con.input.returnPressed.emit()
    qtbot.waitUntil(lambda: "SIM" in con.output.toPlainText().upper(), timeout=3000)
    assert con.history.items == ["*IDN?"]
    assert ("tx", "*IDN?") in [e[1:] for e in con.entries]
    con.send("BOGUS:COMMAND?")
    qtbot.waitUntil(lambda: "(no reply" in con.output.toPlainText(), timeout=3000)

    mem = win.memory
    mem.mcu.setCurrentText("main")
    mem.address.setText("gains")
    mem.length.setValue(16)
    mem.read()
    qtbot.waitUntil(lambda: mem.base == 0x20000014, timeout=3000)
    assert mem.table.rowCount() == 2   # 0x20000010 and 0x20000020 rows
    cell = mem.table.item(0, 4)        # first byte of gains
    cell.setText("7F")
    qtbot.waitUntil(lambda: mem.data[:1] == b"\x7f", timeout=3000)

    # GUI-7: signed shorts in decimal; columns are 2-byte cells, and a typed value writes 2 bytes
    from rdt.memfmt import MemFormat
    mem.set_format(MemFormat("signed", "Short", decimal=True))
    assert mem.table.columnCount() == 9 and mem.table.horizontalHeaderItem(1).text() == "2"
    cell = mem.table.item(0, 2)        # gains, as a short
    assert cell.text() == str(int.from_bytes(mem.data[:2], "little", signed=True))
    cell.setText("-2")
    qtbot.waitUntil(lambda: mem.data[:2] == b"\xfe\xff", timeout=3000)
    ascii_col = mem.table.item(0, 8).text()          # one group of 2 per short, in value order
    assert ascii_col[2 * 3:2 * 3 + 2] == MemFormat("signed", "Short").to_ascii(mem.data[:2], "little")
    mem.set_format(MemFormat("float"))
    assert mem.table.columnCount() == 5
    assert mem.main.settings().value("memory/kind") == "float"


def test_console_history_and_completion(qtbot, win):
    from PySide6.QtTest import QTest
    from rdt.console import CommandHistory

    def key(k, mods=Qt.NoModifier):
        QTest.keyClick(con.output, k, mods)

    connect(qtbot, win)
    con = win.console
    con.send_command("*IDN?")
    con.send_command("MEMory:READ? 0,20000000,12")
    qtbot.waitUntil(lambda: len([e for e in con.entries if e[1] == "rx"]) == 2, timeout=3000)
    assert CommandHistory().items == ["*IDN?", "MEMory:READ? 0,20000000,12"]     # saved for the next launch
    con.input.setText("draft")
    key(Qt.Key_Up)
    assert con.input.text() == "MEMory:READ? 0,20000000,12"
    key(Qt.Key_Up)
    key(Qt.Key_Down)
    key(Qt.Key_Down)
    assert con.input.text() == "draft"

    # Tab: suggestions come from the history and the imported commands only
    from rdt.console import SavedCommand
    win.ws.commands = [SavedCommand("SYSTem:ERRor?"), SavedCommand("MEMory:READ? 0,20000000,4")]
    con.input.setText("syst")
    key(Qt.Key_Tab)
    assert con.input.text() == "SYSTem:ERRor?"
    con.input.setText("mem")
    key(Qt.Key_Tab)                                   # several: their shared start, and a list
    assert con.input.text() == "MEMory:READ? 0,20000000,"
    assert con.completer.model().stringList() == ["MEMory:READ? 0,20000000,12", "MEMory:READ? 0,20000000,4"]
    assert con.completer.popup().isVisible()
    popup = con.completer.popup()                     # wide enough to read the commands
    assert popup.width() >= popup.sizeHintForColumn(0)
    con.completer.activated.emit("MEMory:READ? 0,20000000,4")
    assert con.input.text() == "MEMory:READ? 0,20000000,4" and not con.completer.popup().isVisible()
    con.input.setText("*rs")
    key(Qt.Key_Tab)                                   # *RST is neither in the history nor imported
    assert con.input.text() == "*rs" and not con.completer.popup().isVisible()
    con.input.setText("*i")
    key(Qt.Key_Space, Qt.ControlModifier)             # Ctrl+Space does the same
    assert con.input.text() == "*IDN?"

    # as you type, the best suggestion shows in grey after the cursor; Right takes it
    con.input.setText("")
    QTest.keyClicks(con.output, "sy")
    assert con.input.text() == "sy" and con.output.ghost() == "STem:ERRor?"
    key(Qt.Key_Right)
    assert con.input.text() == "SYSTem:ERRor?" and con.output.ghost() == ""
    con.input.setText("")
    QTest.keyClicks(con.output, "*")
    assert con.output.ghost() == "IDN?"                # the history comes first
    key(Qt.Key_Tab)                                    # a single match: Tab fills it in
    assert con.input.text() == "*IDN?"
    QTest.keyClicks(con.output, "x")
    assert con.output.ghost() == ""                    # nothing starts with *IDN?x
    # the list of matches opens as you type, without Tab
    popup = con.completer.popup()
    con.input.setText("")
    QTest.keyClicks(con.output, "mem")
    assert popup.isVisible() and con.output.ghost() == "ory:READ? 0,20000000,12"
    assert con.completer.model().stringList() == ["MEMory:READ? 0,20000000,12", "MEMory:READ? 0,20000000,4"]
    QTest.keyClick(popup, Qt.Key_Down)
    QTest.keyClick(popup, Qt.Key_Down)                 # highlight the second
    key(Qt.Key_Tab)                                    # Tab takes the highlighted command
    assert con.input.text() == "MEMory:READ? 0,20000000,4" and not popup.isVisible()
    con.input.setText("")
    QTest.keyClicks(con.output, "mem")
    key(Qt.Key_Tab)                                    # none highlighted: Tab takes the first
    assert con.input.text() == "MEMory:READ? 0,20000000,12" and not popup.isVisible()
    con.input.setText("")
    QTest.keyClicks(con.output, "syst")
    QTest.keyClick(popup, Qt.Key_Down)
    key(Qt.Key_Return)                                 # Enter takes the highlighted one, sends nothing
    assert con.input.text() == "SYSTem:ERRor?" and con.entries[-1][2] != "SYSTem:ERRor?"
    con.input.setText("")
    QTest.keyClicks(con.output, "mem")
    key(Qt.Key_Escape)                                 # Esc closes the list
    assert not popup.isVisible() and con.input.text() == "mem"
    QTest.keyClicks(con.output, "o")
    assert popup.isVisible()
    con.input.setText("*IDN?")
    con._typed("*IDN?")                                # a whole command matching only itself: no list
    assert not popup.isVisible()
    con.input.setText("")
    assert con.output.ghost() == con.output.hint

    # the history panel opens beside the transcript, next to the commands list, sends again and deletes
    con.commands_button.setChecked(True)
    con.history_button.setChecked(True)
    dlg = con.history_panel
    assert not dlg.isHidden() and not con.commands.isHidden()
    split = dlg.parentWidget()
    assert split is con.commands.parentWidget() and split.indexOf(dlg) == split.indexOf(con.commands) + 1
    assert [dlg.list.item(i).text() for i in range(dlg.list.count())] == ["MEMory:READ? 0,20000000,12", "*IDN?"]
    dlg.list.setCurrentRow(1)
    dlg.send()
    assert con.history.items == ["MEMory:READ? 0,20000000,12", "*IDN?"] and con.entries[-1][2] == "*IDN?"
    dlg.list.setCurrentRow(0)
    dlg.delete()
    assert CommandHistory().items == ["MEMory:READ? 0,20000000,12"]
    con.history_button.setChecked(False)
    assert dlg.isHidden() and not con.commands.isHidden()


def test_console_commands_list(qtbot, win, tmp_path, monkeypatch):
    con = win.console
    panel = con.commands
    src = tmp_path / "fw.csv"
    src.write_text("command,name,group,description\nMOTor:SPEed?,Speed,Motor,rpm\n*IDN?,Identify,,\n",
                   encoding="utf-8")
    assert panel.import_file(src)                       # offline: the list doesn't need the link
    assert [c.command for c in win.ws.commands] == ["MOTor:SPEed?", "*IDN?"] and win.dirty
    assert con.entries[-1][2] == "imported 2 commands from fw.csv"
    group = panel.tree.topLevelItem(0)
    assert (group.text(0), group.child(0).text(0), group.child(0).toolTip(0)) == ("Motor", "MOTor:SPEed?", "rpm")
    panel.filter.setText("ident")
    assert panel.tree.topLevelItemCount() == 1 and panel.tree.topLevelItem(0).text(0) == "*IDN?"
    panel.filter.clear()
    panel._activated(panel.tree.topLevelItem(0).child(0), 0)
    assert con.input.text() == "MOTor:SPEed?"
    con.input.setText("mot")
    assert con.suggestions()[0] == "MOTor:SPEed?"        # known commands feed Ctrl+Space
    assert panel.import_file(src, replace=False) and con.entries[-1][2].endswith("skipped 2 already listed")
    out = tmp_path / "out.csv"
    assert panel.export_file(out) and out.read_text(encoding="utf-8-sig").count("\n") == 3
    # kept in the workspace
    win.save()
    assert [c.command for c in Workspace.load(win.ws.path).commands] == ["MOTor:SPEed?", "*IDN?"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    panel.clear()
    assert win.ws.commands == [] and panel.tree.topLevelItemCount() == 0


def test_console_font_and_wrap(qtbot, win, tmp_path):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QPlainTextEdit
    from rdt.gui.panels import Console, FONT_MAX
    con = win.console
    assert con.output.lineWrapMode() == QPlainTextEdit.NoWrap     # off by default
    size = con.font_size()
    con.zoom(2)
    assert con.font_size() == size + 2 and con.input.font().pointSize() == size + 2
    wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                        Qt.ControlModifier, Qt.NoScrollPhase, False)
    assert con.eventFilter(con.output.viewport(), wheel)
    assert con.font_size() == size + 1
    con.set_font_size(100)
    assert con.font_size() == FONT_MAX
    con.wrap.setChecked(True)
    assert con.output.lineWrapMode() == QPlainTextEdit.WidgetWidth
    # remembered for the next console, per user
    con.set_font_size(11)
    other = Console(win)
    assert other.font_size() == 11 and other.wrap.isChecked()
    assert (tmp_path / "console.ini").exists()


def test_keyboard_shortcuts(qtbot, win):
    rows = win.shortcut_rows()
    assert ("Target", "Connect or disconnect", "F5") in rows
    assert ("Watch", "Add selected to chart", "Ctrl+G") in rows
    assert ("Console", "Complete the command; with several matches, list them all", "Tab / Ctrl+Space") in rows
    dlg = win.show_shortcuts()
    from PySide6.QtWidgets import QTableWidget
    assert dlg.findChild(QTableWidget).rowCount() == len(rows)
    dlg.close()


def test_console_transcript(qtbot, win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPlainTextEdit
    from rdt.console import hex_to_text, text_to_hex

    connect(qtbot, win)
    con = win.console
    assert con.entries[-1][1:] == ("note", "Connected \u00b7 " + win.ws.config.connection.address)
    con.send("*IDN?")
    qtbot.waitUntil(lambda: con.entries[-1][1] == "rx", timeout=3000)
    con.send("BOGUS?")
    qtbot.waitUntil(lambda: con.entries[-1][1] == "error", timeout=3000)
    text = con.output.toPlainText().splitlines()
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3} TX  BOGUS\?", text[-3])
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3} ERR \(no reply.*", text[-2])
    assert text[-1] == win.ws.config.connection.address + " > "   # the prompt is the last line
    con.timestamps.setChecked(False)
    assert con.output.toPlainText().splitlines()[-3] == "TX  BOGUS?"
    con.input.setText("half typed")
    con.log("line one\nline two")                    # a multi-line reply lines up under its text
    assert con.output.toPlainText().splitlines()[-3:] == [
        "RX  line one", "    line two", win.ws.config.connection.address + " > half typed"]
    assert con.input.text() == "half typed"            # a reply arriving keeps what you are typing
    con.input.setText("")

    def fragments(block):
        it, out = block.begin(), []
        while not it.atEnd():
            f = it.fragment()
            out.append((f.text(), f.charFormat().foreground().color().name()))
            it += 1
        return out

    blocks = {kind: fragments(con.output.document().findBlockByNumber(i))
              for i, (_, kind, _) in enumerate(con.entries) if kind in ("tx", "error", "note")}
    assert blocks["tx"][0] == ("TX  ", "#3fb950") and blocks["tx"][1][1] == "#7ee787"
    assert blocks["error"][0] == ("ERR ", "#f85149") and blocks["error"][1][1] == "#ff7b72"
    assert blocks["note"][0] == ("SYS ", "#8b949e")
    assert "Tab" in con.output.hint

    # search: every match highlighted, Enter and Shift+Enter step, newest first
    con.send("BOGUS?")
    qtbot.waitUntil(lambda: sum(kind == "error" for _, kind, _ in con.entries) == 2, timeout=3000)
    con.show_find()
    con.find_text.setText("bogus")
    assert con.find_count.text() == "2 of 2"        # the command, sent twice
    con.find_next()
    assert con.find_count.text() == "1 of 2"
    con.find_previous()
    assert con.find_count.text() == "2 of 2" and len(con.output.extraSelections()) == 2
    con.find_text.setText("nothing here")
    assert con.find_count.text() == "no match"
    con.hide_find()
    assert not con.output.extraSelections()

    # hex and text
    dlg = con.show_converted("Hex selection as text", hex_to_text, "48 65 6C 6C 6F")
    assert dlg.findChild(QPlainTextEdit).toPlainText() == "Hello"
    dlg.close()
    dlg = con.show_converted("Selection as hex", text_to_hex, "Hi")
    assert dlg.findChild(QPlainTextEdit).toPlainText() == "48 69"
    dlg.close()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    assert con.show_converted("Hex selection as text", hex_to_text, "Hello") is None
    assert "not hex" in warnings[0]

    # session log
    log = tmp_path / "console.txt"
    assert con.start_log(log)
    con.send("*IDN?")
    qtbot.waitUntil(lambda: con.entries[-1][1] == "rx", timeout=3000)
    con.stop_log()
    lines = [line[24:] for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines[:3] == ["# log started", f"# logging to {log}", "> *IDN?"]   # the file keeps its marks
    assert lines[3].startswith("< RDT,Simulator") and lines[-1] == "# log stopped"

    # Ctrl+K clears
    win.show()
    win.docks["Console"].raise_()
    qtbot.waitExposed(win)
    con.input.setFocus()
    qtbot.waitUntil(con.input.hasFocus, timeout=3000)
    from rdt.console import SavedCommand
    win.ws.commands = [SavedCommand("*IDN?")]
    con.input.setText("*i")
    qtbot.keyClick(con.input, Qt.Key_Tab)             # a real Tab completes and keeps the focus
    assert con.input.text() == "*IDN?" and con.input.hasFocus()
    qtbot.keyClick(con.input, Qt.Key_K, Qt.ControlModifier)
    assert con.output.toPlainText() == win.ws.config.connection.address + " > *IDN?" and not con.entries


def test_watch_tabs(qtbot, win, monkeypatch, tmp_path):
    tabs = win.watch_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Motor", "System"]
    motor, system = tabs.trees
    assert win.watch_tree is motor
    # main:motor.mode is in both tabs: one item, read once per poll, shown in each.
    assert motor.states["main:motor.mode"].watch is system.states["main:motor.mode"].watch
    reads = []
    real = win.ws.read_many
    monkeypatch.setattr(win.ws, "read_many", lambda t, items, *a, **k: reads.append(
        [w.expr for w in items]) or real(t, items, *a, **k))
    connect(qtbot, win)
    # the tab in the back keeps polling
    wait_value(qtbot, win, "main:tick_count", lambda v: v.startswith("0x"))
    qtbot.waitUntil(lambda: system.states["main:motor.mode"].item.text(COL_VALUE) != "", timeout=3000)
    assert all(r.count("main:motor.mode") <= 1 for r in reads)

    # new settings for a shared item show in both tabs
    motor.set_format("main:motor.mode", "hex")
    assert system.states["main:motor.mode"].watch.display.format == "hex"

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Scratch", True))
    tree = tabs.new_tab()
    assert win.watch_tree is tree and tabs.tabText(2) == "Scratch"
    assert tree.add_watch("main:gains") is not None
    tabs.new_tab("scratch")   # a name in use is refused
    assert tabs.count() == 3
    tabs.rename_tab(2, "Gains")
    tabs.tabBar().moveTab(2, 0)
    assert [t.name for t in win.ws.tabs] == ["Gains", "Motor", "System"]
    path = tmp_path / "tabs.rdtw"
    assert win._save_to(path)
    back = Workspace.load(path)
    assert [(t.name, len(t.watches)) for t in back.tabs] == [("Gains", 1), ("Motor", 4), ("System", 3)]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    tabs.close_tab(0)
    tabs.close_tab(0)
    assert [t.name for t in win.ws.tabs] == ["System"]
    tabs.close_tab(0)   # the last tab stays
    assert tabs.count() == 1


def test_an_old_workspace_with_buttons_opens(qtbot, tmp_path):
    path = tmp_path / "old.rdtw"
    path.write_text('[[mcu]]\nid = 0\nname = "main"\n\n[[watch]]\nexpr = "main:tick_count"\n\n'
                    '[[button]]\nname = "Who"\ncommand = "*IDN?"\n')
    w = MainWindow()
    qtbot.addWidget(w, before_close_func=lambda w: setattr(w, "dirty", False))
    assert w.open_path(path)
    assert [w.watch_tabs.tabText(i) for i in range(w.watch_tabs.count())] == ["Watch"]
    assert "main:tick_count" in w.watch_tree.states
    assert "no longer supported" in w.message.text()


def test_profile_dialog_preview_and_apply(qtbot, win):
    dlg = ProfileDialog(win.ws.config.profile, win.device, win)
    qtbot.addWidget(dlg)
    assert "MEMory:READ? 0,20000000,16" in dlg.preview.text()
    dlg.edits["read"].setText("MEM:RD {addr}")
    assert "needs {addr} and {len}" in dlg.preview.text()
    assert not dlg.ok.isEnabled()
    dlg.edits["read"].setText("MEM:RD? {mcu},{addr},{len}")
    dlg.edits["terminator"].setText("\\r\\n")
    p = dlg.result_profile()
    assert p.terminator == "\r\n" and p.read == "MEM:RD? {mcu},{addr},{len}"
    win.apply_profile(p)
    assert win.ws.config.profile_path is None
    assert win.dirty


def test_profile_test_read(qtbot, win):
    connect(qtbot, win)
    dlg = ProfileDialog(win.ws.config.profile, win.device, win)
    qtbot.addWidget(dlg)
    dlg.test_addr.setText("0x20000010")
    dlg.test_len.setValue(4)
    dlg.test()
    qtbot.waitUntil(lambda: "OK:" in dlg.test_result.text(), timeout=3000)


def test_mcu_dialog(qtbot, win):
    dlg = McuDialog(win.ws.config.mcus, win)
    qtbot.addWidget(dlg)
    assert [m.name for m in dlg.result_mcus()] == ["main", "motorctl"]
    assert dlg.result_mcus()[0].ranges   # GPIOA kept
    assert dlg.table.item(0, 4).text() == "firmware_name" and dlg.result_mcus()[0].identity == "firmware_name"
    dlg.table.item(0, 1).setText("motorctl")
    with pytest.raises(Exception, match="unique"):
        dlg.result_mcus()


def test_elf_rebuild_remaps(qtbot, win, tmp_path):
    elf = tmp_path / "main.elf"
    shutil.copy(win.ws.config.mcus[0].elf, elf)
    win.ws.config.mcus[0].elf = elf
    connect(qtbot, win)
    wait_value(qtbot, win, "main:tick_count", lambda v: True)
    st = win.watch_tabs.trees[1].states["main:tick_count"]   # in the tab that is not in front
    rebuilt = ROOT / "tests" / "elf" / "corpus-clang-v2.elf"
    if not rebuilt.exists():
        pytest.skip("no rebuild variant ELF")
    shutil.copy(rebuilt, elf)
    load_symbols(elf)   # cached, so the reload on the device thread is quick
    os.utime(elf, (time.time() - 5,) * 2)   # settled: the linker is done with it
    win._check_elfs()
    qtbot.waitUntil(lambda: st.gone or st.note is not None, timeout=5000)


def test_link_down_greys_panels(qtbot, win, sim):
    connect(qtbot, win)
    from PySide6.QtTest import QTest
    assert win.console.output.input_enabled
    win.disconnect()
    qtbot.waitUntil(lambda: win.bar.connect_button.text() == "Connect", timeout=3000)
    con = win.console
    assert not con.output.input_enabled and con.output.hint == "not connected"
    QTest.keyClicks(con.output, "abc")                 # nothing can be typed while disconnected
    assert con.input.text() == ""
    assert win.console.commands_button.isEnabled()      # the list, search and log work offline
    assert win.bar.transport.isEnabled()


# --- M4: chart, statistics, CSV log ------------------------------------------------------------

def test_one_poll_reads_many_items(qtbot, win, monkeypatch):
    polls = []
    real = win.ws.read_many
    monkeypatch.setattr(win.ws, "read_many",
                        lambda t, items, *a, **kw: (polls.append([w.expr for w in items]), real(t, items, *a, **kw))[1])
    connect(qtbot, win)
    qtbot.waitUntil(lambda: len(polls) >= 3, timeout=5000)
    # on connect every item is read, in one poll
    assert set(polls[0]) == {w.expr for w in win.ws.watches}


def test_chart_follows_values(qtbot, win):
    connect(qtbot, win)
    tree, chart = win.watch_tree, win.chart
    tree.add_watch("motorctl:adc_log.count")   # a counter in the simulator
    wait_value(qtbot, win, "motorctl:adc_log.count", lambda v: True)
    tree.chart("motorctl:adc_log.count")
    tree.chart("main:motor.speed")
    assert set(chart.series) == {"motorctl:adc_log.count", "main:motor.speed"}
    # a different unit gets its own axis; the unit labels it
    assert chart.series["main:motor.speed"].unit == "rpm"
    assert chart.series["main:motor.speed"].axis != chart.series["motorctl:adc_log.count"].axis
    s = chart.series["motorctl:adc_log.count"]
    qtbot.waitUntil(lambda: len(s.trace) >= 4, timeout=5000)
    t, v = s.trace.t, s.trace.v
    assert (t[1:] >= t[:-1]).all() and v[-1] > v[0]
    qtbot.waitUntil(lambda: s.curve.xData is not None and len(s.curve.xData) >= 4, timeout=2000)
    # following: the view ends now; paused: it stays put
    x0, x1 = chart.views[0].viewRange()[0]
    assert abs(x1 - time.time()) < 1 and abs((x1 - x0) - chart.window_s) < 0.5
    chart.set_paused(True)
    before = chart.views[0].viewRange()[0]
    qtbot.wait(300)
    assert chart.views[0].viewRange()[0] == before
    # cursors read every signal
    chart.cursor_button.setChecked(True)
    chart.cursors[0].setValue(t[1])
    chart.cursors[1].setValue(t[-1])
    assert s.item.text(5) != "" and "Δt" in chart.readout.text()
    chart.set_paused(False)
    assert chart.series["main:motor.speed"].item.text(2) == "1.5 rpm"


def test_charted_field_of_a_collapsed_struct_is_read(qtbot, win):
    connect(qtbot, win)
    tree = win.watch_tree
    tree.add_watch("main:motor")
    st = tree.states["main:motor"]
    qtbot.waitUntil(lambda: st.aggregate is not None, timeout=5000)
    assert not st.item.isExpanded()
    tree.chart("main:motor.mode")
    s = win.chart.series["main:motor.mode"]
    qtbot.waitUntil(lambda: len(s.trace) >= 3, timeout=5000)
    assert s.trace.last() == 1   # MODE_RUN


def test_chart_layout_is_saved(qtbot, win, tmp_path):
    connect(qtbot, win)
    wait_value(qtbot, win, "main:motor.current_ma", lambda v: True)
    win.watch_tree.chart("main:motor.current_ma")
    win.chart.set_axis("main:motor.current_ma", 3)
    win.chart.set_color("main:motor.current_ma", "#123456")
    win.chart.window.setCurrentIndex(5)   # 2 min
    assert win.dirty
    out = tmp_path / "saved.rdtw"
    assert win._save_to(out)
    ws = Workspace.load(out)
    assert ws.chart == {"window_s": 120, "series": [{"expr": "main:motor.current_ma", "axis": 3, "color": "#123456",
                                                      "unit": "A"}]}
    win.disconnect()
    assert win.open_path(out)
    s = win.chart.series["main:motor.current_ma"]
    assert (s.axis, s.color, s.unit, win.chart.window_s) == (3, "#123456", "A", 120)


def test_statistics(qtbot, win):
    from rdt.gui.variables import COL_COUNT, COL_MAX, COL_MEAN, COL_MIN
    connect(qtbot, win)
    tree = win.watch_tree
    item = tree.states["main:motor.speed"].item
    qtbot.waitUntil(lambda: item.text(COL_COUNT) != "" and int(item.text(COL_COUNT)) >= 3, timeout=5000)
    assert item.text(COL_MIN) == item.text(COL_MAX) == item.text(COL_MEAN) == "1.5"
    assert tree.isColumnHidden(COL_MIN)
    tree.show_stats(True)
    assert not tree.isColumnHidden(COL_MIN)
    tree.reset_stats("main:motor.speed")
    assert item.text(COL_COUNT) == ""
    qtbot.waitUntil(lambda: item.text(COL_COUNT) != "", timeout=3000)


def test_csv_log(qtbot, win, tmp_path):
    import csv
    path = tmp_path / "log.csv"
    assert not win.start_log(path)   # not connected yet
    connect(qtbot, win)
    wait_value(qtbot, win, "motorctl:temperature", lambda v: True)
    assert win.start_log(path)
    assert win.bar.log_button.isChecked()
    qtbot.waitUntil(lambda: win.log.rows >= 4, timeout=5000)
    win.stop_log()
    assert not win.bar.log_button.isChecked()
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    head = rows[0]
    assert head[:3] == ["time", "seconds", "main:motor.speed [rpm]"]
    assert "main:motor.current_ma [A]" in head and "motorctl:temperature [degC]" in head and head[-1] == "event"
    first = dict(zip(head, rows[1]))   # the first row has everything
    assert first["main:motor.speed [rpm]"] == "1.5" and first["main:motor.current_ma [A]"] == "-0.12"
    assert first["main:motor.mode"] == "1" and first["main:tick_count"] != ""
    later = [dict(zip(head, r)) for r in rows[2:]]
    assert any(r["main:motor.speed [rpm]"] for r in later)
    assert all(r["motorctl:temperature [degC]"] == "" for r in later)   # manual: read once only


def test_csv_log_marks_link_loss(qtbot, win, tmp_path):
    from rdt.errors import LinkDown
    from rdt.workspace import Reading
    connect(qtbot, win)
    wait_value(qtbot, win, "main:motor.speed", lambda v: True)
    path = tmp_path / "log.csv"
    assert win.start_log(path)
    w = win.ws.watch("main:motor.speed")
    win.watch_tree.chart("main:motor.speed")
    s = win.chart.series["main:motor.speed"]
    qtbot.waitUntil(lambda: len(s.trace) >= 1, timeout=3000)
    win._log_readings([Reading(w, time.time(), error=LinkDown("down"))])
    win.chart.feed([Reading(w, time.time(), error=LinkDown("down"))])
    assert s.trace.v[-1] != s.trace.v[-1]   # NaN: a gap in the line
    qtbot.waitUntil(lambda: win._log_link_up, timeout=3000)
    win.stop_log()
    events = [r[-1] for r in __import__("csv").reader(path.read_text(encoding="utf-8-sig").splitlines())]
    assert "link down" in events and "link up" in events


# --- M5: read-only default (WS-4), reopen last workspace (WSP-3), ELF identity (SYM-8) -------

def test_read_only_default(qtbot, win):
    connect(qtbot, win)
    tree = win.watch_tree
    assert not win.read_only_action.isChecked()
    win.read_only_action.trigger()
    assert win.ws.read_only and win.dirty
    tree.write("main:motor.target", "1500")
    assert "every item read-only" in win.message.text()
    st = tree.states["main:motor.target"]
    dlg = WatchDialog(st.watch, win)
    qtbot.addWidget(dlg)
    assert dlg.read_only.currentText() == "Workspace default"
    dlg.read_only.setCurrentText("Writable")
    assert dlg.result_watch().read_only is False
    dlg.read_only.setCurrentText("Read-only")
    assert dlg.result_watch().read_only is True
    win.read_only_action.trigger()
    assert not win.ws.read_only


def test_reopen_last_workspace(qtbot, win, bench, tmp_path):
    other = MainWindow()
    qtbot.addWidget(other, before_close_func=lambda w: setattr(w, "dirty", False))
    assert other.reopen_last() and other.ws.path == bench      # win opened it last
    moved = tmp_path / "moved.rdtw"
    assert other._save_to(moved)
    third = MainWindow()
    qtbot.addWidget(third, before_close_func=lambda w: setattr(w, "dirty", False))
    assert third.reopen_last() and third.ws.path == moved      # the last one saved
    moved.unlink()
    fourth = MainWindow()
    qtbot.addWidget(fourth, before_close_func=lambda w: setattr(w, "dirty", False))
    assert not fourth.reopen_last() and fourth.ws.path is None
    assert "is gone" in fourth.message.text()


def test_elf_identity(qtbot, win, tmp_path, monkeypatch):
    connect(qtbot, win)
    qtbot.waitUntil(lambda: "firmware_name matches" in win.console.output.toPlainText(), timeout=3000)
    assert not win.allow_action.isEnabled()
    assert "main: ELF matches the firmware; motorctl: ELF matches the firmware" in win.message.text()
    assert "MCU motorctl: 2 flash blocks (88 bytes) match motorctl.elf" in win.console.output.toPlainText()
    assert win.bar.image.text() == "Image \u2713 verified" and "motorctl: match" in win.bar.image.toolTip()
    win.device.target.mcus["main"].identity_stale = True   # the link came back: checked again by itself
    qtbot.waitUntil(lambda: win.device.target.stale_identities() == [], timeout=3000)   # by the GUI or a poll
    qtbot.waitUntil(lambda: win.bar.image.text() == "Image \u2713 verified", timeout=3000)
    elf = tmp_path / "main.elf"
    elf.write_bytes(win.ws.config.mcus[0].elf.read_bytes().replace(b"rdt-main\0", b"rdt-mXin\0"))
    win.ws.config.mcus[0].elf = elf
    load_symbols(elf)   # parse its DWARF here: a long parse on the device thread can starve under the Qt loop
    win.disconnect()
    qtbot.waitUntil(lambda: win.device.target is None, timeout=3000)
    connect(qtbot, win)
    qtbot.waitUntil(lambda: win.allow_action.isEnabled(), timeout=3000)
    assert "does not match the firmware on main" in win.message.text()
    qtbot.waitUntil(lambda: win.bar.image.text() == "Image \u2717 mismatch", timeout=3000)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.watch_tree.write("main:motor.target", "1500")
    qtbot.waitUntil(lambda: "does not match" in win.message.text(), timeout=3000)
    win.allow_action.trigger()
    qtbot.waitUntil(lambda: not win.allow_action.isEnabled(), timeout=3000)
    assert "Reads and writes allowed to main" in win.message.text()
    win.disconnect()
    qtbot.waitUntil(lambda: not win.bar._image.isVisible(), timeout=3000)


def test_commands_import_and_export_buttons(qtbot, win, tmp_path, monkeypatch):
    """The real buttons: clicked() must not hand its checked flag to import_file as the path."""
    from PySide6.QtWidgets import QFileDialog, QPushButton
    panel = win.console.commands
    buttons = {b.text(): b for b in panel.findChildren(QPushButton)}
    src = tmp_path / "fw.txt"
    src.write_text("*IDN?\nMOTor:SPEed?\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(src), ""))
    buttons["Import..."].click()
    assert [c.command for c in win.ws.commands] == ["*IDN?", "MOTor:SPEed?"]
    out = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    buttons["Export..."].click()
    assert out.read_text(encoding="utf-8-sig").splitlines()[1:] == ["*IDN?,,,", "MOTor:SPEed?,,,"]


def test_polling_can_be_turned_off_and_polled_by_hand(qtbot, win):
    connect(qtbot, win)
    tree = win.watch_tree
    far = time.monotonic() + 3600
    assert tree.due(far)                                   # everything is due an hour from now
    win.bar.polling.setChecked(False)
    assert not win.watch_tabs.polling and not win.polling_action.isChecked()
    assert not win.bar.poll.isEnabled()
    assert tree.due(far, win.watch_tabs.polling) == []     # nothing is read while polling is off
    st = tree.states["main:motor.speed"]
    qtbot.waitUntil(lambda: not win.device.busy("poll"), timeout=3000)
    last = st.last_done
    win.bar.poll_now.click()                               # Poll now still reads every item once
    qtbot.waitUntil(lambda: st.last_done != last, timeout=3000)
    win.polling_action.trigger()                           # Watch > Poll automatically (F7) turns it back on
    assert win.watch_tabs.polling and win.bar.polling.isChecked() and win.bar.poll.isEnabled()


def test_com_ports_show_their_description(qtbot, win, monkeypatch):
    import rdt.gui.app as app
    monkeypatch.setattr(app, "serial_ports", lambda: [("COM1", ""), ("COM3", "GAC serial port (COM3)")])
    bar = win.bar
    bar.transport.setCurrentText("serial")
    assert [bar.com.itemText(i) for i in range(bar.com.count())] == ["COM1", "COM3 - GAC serial port (COM3)"]
    bar.com.setCurrentIndex(1)
    assert bar.connection().address == "COM3"          # the port, not its description
    bar.com.setEditText("COM9")                        # a port typed by hand
    assert bar.connection().address == "COM9"
    bar.set_port("COM3")                               # a saved port shows its description
    assert bar.com.currentText() == "COM3 - GAC serial port (COM3)"
    assert app.port_name("COM3 - GAC serial port (COM3)") == "COM3" and app.port_name(" /dev/ttyUSB0 ") == "/dev/ttyUSB0"
