"""GUI tests: the main window against the simulated device, run offscreen with pytest-qt."""

import dataclasses
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
from PySide6.QtGui import QAction  # noqa: E402

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


def card(win, mcu):
    return win.symbols.cards[mcu]


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
    # one button shows the state and connects or disconnects (GUI-11)
    where = win.ws.config.connection.address
    qtbot.waitUntil(lambda: win.bar.connect_button.text() == "Disconnected \u00b7 " + where, timeout=3000)
    assert "Click to connect" in win.bar.connect_button.toolTip()
    assert win.bar.port.value() == int(win.ws.config.connection.address.rsplit(":", 1)[1])
    connect(qtbot, win)
    assert win.bar.connect_button.text() == "Connected \u00b7 " + win.ws.config.connection.address
    assert "Click to disconnect" in win.bar.connect_button.toolTip()
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
    qtbot.waitUntil(lambda: "above its maximum" in win.banner.error, timeout=3000)

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
    sb = win.symbols.browser("main")
    assert sb.mcu.isHidden() and not sb.isHidden() and card(win, "motorctl").browser.isHidden()
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
    assert mem.table.isColumnHidden(8)               # ASCII column only beside hex bytes
    assert not mem.table.horizontalHeader().stretchLastSection()   # last value column not stretched
    mem.set_format(MemFormat("float"))
    assert mem.table.columnCount() == 5
    mem.set_format(MemFormat())
    assert not mem.table.isColumnHidden(16)
    mem.set_format(MemFormat("float"))
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
    assert (tmp_path / "ui.toml").exists() and "font_size = 11" in (tmp_path / "ui.toml").read_text()


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
    from rdt.gui.panels import LINE_COLORS, LINE_TAGS   # the theme's colours (dark or light)
    assert blocks["tx"][0] == ("TX  ", LINE_TAGS["tx"][1]) and blocks["tx"][1][1] == LINE_COLORS["tx"]
    assert blocks["error"][0] == ("ERR ", LINE_TAGS["error"][1]) and blocks["error"][1][1] == LINE_COLORS["error"]
    assert blocks["note"][0] == ("SYS ", LINE_TAGS["note"][1])
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


def test_profile_dialog_loads_a_file(qtbot, win, tmp_path, monkeypatch):
    path = tmp_path / "gac.toml"
    path.write_text('[scpi]\nname = "RDT GAC"\nread = "SERV:MEM:READ? {mcu},{addr},{len}"\n'
                    'write = "SERV:MEM:WRIT {mcu},{addr},{value},{size}"\nmax_response = 500\n')
    assert not any(a.text().startswith("Load profile") for a in win.findChildren(QAction))
    dlg = ProfileDialog(win.ws.config.profile, win.device, win)
    qtbot.addWidget(dlg)
    dlg.load_file(str(path))
    assert dlg.edits["name"].text() == "RDT GAC" and dlg.edits["max_response"].text() == "500"
    assert "SERV:MEM:READ? 0,20000000,16" in dlg.preview.text()
    assert "gac.toml" in dlg.file_label.text()
    win.apply_profile(dlg.result_profile(), win.ws.config.profile_path)
    assert win.ws.config.profile.name == "RDT GAC"
    bad = tmp_path / "bad.toml"
    bad.write_text("[scpi]\nnope = 1\n")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a[2]))
    dlg.load_file(str(bad))
    assert "unknown [scpi] keys" in warned[0] and dlg.edits["name"].text() == "RDT GAC"


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
    assert dlg.table.columnCount() == 6   # no identity variable column
    dlg.table.item(1, 2).setText("MC")
    dlg.table.item(1, 3).setText("#D9822B")
    m = dlg.result_mcus()[1]
    assert (m.short, m.color) == ("MC", "#d9822b")
    dlg.table.item(1, 2).setText("MAIN")
    with pytest.raises(Exception, match="short names must be unique"):
        dlg.result_mcus()
    dlg.table.item(1, 2).setText("bad name")
    with pytest.raises(Exception, match="short name"):
        dlg.result_mcus()
    dlg.table.item(1, 2).setText("")
    dlg.table.item(0, 1).setText("motorctl")
    with pytest.raises(Exception, match="unique"):
        dlg.result_mcus()


def test_mcu_dialog_shows_the_whole_elf_path(qtbot, win):
    """The ELF path sits on one line, elided in the middle, with the full path as tooltip."""
    from PySide6.QtCore import Qt
    dlg = McuDialog(win.ws.config.mcus, win)
    qtbot.addWidget(dlg)
    assert not dlg.table.wordWrap() and dlg.table.textElideMode() == Qt.ElideMiddle
    item = dlg.table.item(0, dlg.COL_ELF)
    assert item.text() and item.toolTip() == item.text()
    item.setText(r"C:\work\fw\Objects\new.axf")
    assert item.toolTip() == r"C:\work\fw\Objects\new.axf"


def test_watch_rows_show_mcu_tags_and_flash_changes(qtbot, win, tmp_path):
    from rdt.gui.style import TAG_ROLE
    from rdt.gui.variables import COL_NAME
    tree = win.watch_tree
    item = tree.states["main:motor.speed"].item
    assert item.text(COL_NAME) == "motor.speed" and item.toolTip(COL_NAME).startswith("main:motor.speed")
    assert item.data(COL_NAME, TAG_ROLE) == ("MAIN", "#7b61d6")
    win.ws.config.mcus[0].short, win.ws.config.mcus[0].color = "M", "#123456"
    tree.retag()
    assert item.data(COL_NAME, TAG_ROLE) == ("M", "#123456")
    path = tmp_path / "tags.rdtw"
    win.ws.save(path)
    text = path.read_text()
    assert re.search(r'short\s*= "M"', text) and re.search(r'color\s*= "#123456"', text)
    assert Workspace.load(path).config.mcus[0].short == "M"
    connect(qtbot, win)
    wait_value(qtbot, win, "main:motor.speed", lambda v: v.endswith("rpm"))
    st = tree.states["main:motor.speed"]
    row = st.shown["main:motor.speed"]
    tree._fill(st, [row])
    assert not tree.flashing   # the same value again
    tree._fill(st, [dataclasses.replace(row, text="1234.5 rpm")])
    assert "main:motor.speed" in tree.flashing
    assert item.background(COL_VALUE).color().isValid() and item.background(COL_VALUE).style() != Qt.NoBrush
    tree.fade(time.monotonic() + 5)
    assert not tree.flashing and item.background(COL_VALUE).style() == Qt.NoBrush


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
    qtbot.waitUntil(lambda: win.bar.connect_button.text().startswith("Disconnected"), timeout=3000)
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
    qtbot.waitUntil(lambda: "3 flash blocks (112 bytes) match main.elf" in win.console.output.toPlainText(), timeout=3000)
    assert not any("mismatch" in a.text() for a in win.findChildren(QAction))   # no way around it in the GUI
    assert "main: ELF matches the firmware; motorctl: ELF matches the firmware" in win.message.text()
    assert "MCU motorctl: 2 flash blocks (88 bytes) match motorctl.elf" in win.console.output.toPlainText()
    assert "\u2713 main.elf" in card(win, "main").elf.text() and "\u2713 motorctl.elf" in card(win, "motorctl").elf.text()
    assert win.banner.isHidden()
    win.device.target.mcus["main"].identity_stale = True   # the link came back: checked again by itself
    qtbot.waitUntil(lambda: win.device.target.stale_identities() == [], timeout=3000)   # by the GUI or a poll
    qtbot.waitUntil(lambda: "\u2713 main.elf" in card(win, "main").elf.text(), timeout=3000)
    elf = tmp_path / "main.elf"
    elf.write_bytes(win.ws.config.mcus[0].elf.read_bytes().replace(b"rdt-main\0", b"rdt-mXin\0"))
    win.ws.config.mcus[0].elf = elf
    load_symbols(elf)   # parse its DWARF here: a long parse on the device thread can starve under the Qt loop
    win.disconnect()
    qtbot.waitUntil(lambda: win.device.target is None, timeout=3000)
    connect(qtbot, win)
    qtbot.waitUntil(lambda: "does not match the firmware on main" in win.banner.text.text(), timeout=3000)
    qtbot.waitUntil(lambda: "\u2717 main.elf does not match" in card(win, "main").elf.text(), timeout=3000)
    assert not win.banner.isHidden() and not win.banner.load_button.isHidden()
    assert "1 of 3 flash blocks" in win.banner.toolTip() and not win.banner.toggleViewAction().isVisible()
    assert "does not match" not in win.message.text()   # one error bar, nothing red at the bottom
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.watch_tree.write("main:motor.target", "1500")
    qtbot.waitUntil(lambda: "write main:motor.target" in win.banner.error, timeout=3000)
    assert win.banner.text.text().count("\n") == 1 and not win.banner.close_button.isHidden()
    win.disconnect()
    qtbot.waitUntil(lambda: "not connected" in card(win, "main").link.text(), timeout=3000)
    qtbot.waitUntil(win.banner.isHidden, timeout=3000)   # "Disconnected" clears the write error


def test_error_bar(qtbot, win):
    """Errors show in the bar under the toolbar, not in the status bar; closing it or a later
    successful action clears them."""
    assert win.banner.isHidden()
    win.status("Cannot connect: no such port", error=True)
    assert not win.banner.isHidden() and win.banner.text.text() == "Cannot connect: no such port"
    assert win.message.text() == "" and win.banner.load_button.isHidden()
    win.banner.close_button.click()
    assert win.banner.isHidden()
    win.status("poll: timeout", error=True)
    win.status("Saved x.rdtw")
    assert win.banner.isHidden() and win.message.text() == "Saved x.rdtw"


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


def test_polling_can_be_turned_off_and_polled_by_hand(qtbot, win, monkeypatch):
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
    # Count the reads rather than compare last_done: on Python 3.11 under Windows time.monotonic()
    # ticks every ~16 ms, so a fast read can finish on the same tick as the one before it.
    done = []
    monkeypatch.setattr(tree, "_done", lambda s, result, f=tree._done: (done.append(s), f(s, result)))
    win.bar.poll_now.click()                               # Poll now still reads every item once
    qtbot.waitUntil(lambda: st in done, timeout=3000)
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


def test_settings_files_import_and_export(qtbot, win, bench, tmp_path, monkeypatch):
    file_menu = win.menuBar().actions()[0].menu()
    subs = {a.text().replace("&", ""): a.menu() for a in file_menu.actions() if a.menu()}
    for name in ("Import", "Export"):
        assert [a.text().replace("&", "") for a in subs[name].actions()] == \
               ["Watch list...", "Commands...", "Memory map...", "Interface settings..."]
    # the workspace was saved in three files
    assert (tmp_path / "bench.watch.toml").is_file()
    exported = tmp_path / "out.watch.toml"
    assert win.export_watches(exported) and "[[tab]]" in exported.read_text()
    tabs = [t.name for t in win.ws.tabs]
    win.ws.remove_tab(tabs[0])
    win.watch_tabs.rebuild()
    assert win.import_watches(exported, replace=False)
    assert win.watch_tabs.count() == len(tabs) and win.dirty
    assert win.import_watches(exported, replace=True) and [t.name for t in win.ws.tabs] == tabs
    assert "Imported" in win.message.text()

    memory = tmp_path / "map.memory.toml"
    memory.write_text('[[ranges]]\nname = "GPIOA"\nstart = 0x40020000\nsize = 0x400\n')
    assert win.import_memory(memory, replace=True) and [r.name for r in win.ws.memory_ranges()] == ["GPIOA"]
    out = tmp_path / "out.memory.toml"
    assert win.export_memory(out) and "GPIOA" in out.read_text()

    win.console.set_font_size(9)
    ui = tmp_path / "mine.toml"
    assert win.export_ui(ui) and "font_size = 9" in ui.read_text()
    ui.write_text('[console]\nfont_size = 15\nwrap = true\n\n[memory]\nkind = "float"\n')
    assert win.import_ui(ui)
    assert win.console.font_size() == 15 and win.console.wrap.isChecked()
    assert win.memory.format.kind == "float"
    ui.write_text("[console]\nsize = 1\n")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    assert not win.import_ui(ui)
    assert "not an interface setting" in warnings[0]


def test_old_ini_settings_move_to_ui_toml(qtbot, tmp_path):
    from PySide6.QtCore import QSettings
    gui = QSettings(str(tmp_path / "gui.ini"), QSettings.IniFormat)
    gui.setValue("last_workspace", str(tmp_path / "x.rdtw"))
    gui.setValue("memory/kind", "signed")
    gui.setValue("memory/decimal", "true")
    gui.sync()
    con = QSettings(str(tmp_path / "console.ini"), QSettings.IniFormat)
    con.setValue("font_size", 13)
    con.setValue("wrap", True)
    con.sync()
    w = MainWindow()
    qtbot.addWidget(w, before_close_func=lambda w: setattr(w, "dirty", False))
    assert w.ui.values == {"session/last_workspace": str(tmp_path / "x.rdtw"), "memory/kind": "signed",
                           "memory/decimal": True, "console/font_size": 13, "console/wrap": True}
    assert (tmp_path / "ui.toml").is_file() and w.console.font_size() == 13


def test_link_down_when_the_device_stops_answering(qtbot, win, sim):
    """A hung board keeps its COM port open: after a few reads with no reply the box turns red
    and the MCU cards say link down, and the first reply brings both back (LNK-9)."""
    connect(qtbot, win)
    qtbot.waitUntil(lambda: "✓ main.elf" in card(win, "main").elf.text(), timeout=3000)
    sim.device.frozen = True
    win.read_all()
    where = win.ws.config.connection.address
    qtbot.waitUntil(lambda: win.bar.connect_button.text() == "Link down · " + where, timeout=8000)
    assert "answered none" in win.bar.connect_button.toolTip()
    assert "link down" in card(win, "main").link.text()
    assert "did not answer the last" in win.console.output.toPlainText()
    sim.device.frozen = False
    win.read_all()
    qtbot.waitUntil(lambda: win.bar.connect_button.text() == "Connected · " + where, timeout=8000)
    qtbot.waitUntil(lambda: "✓ main.elf" in card(win, "main").elf.text(), timeout=5000)


def test_dark_theme_is_remembered(qtbot, win):
    from PySide6.QtWidgets import QApplication
    from rdt.gui.style import is_dark
    system = QApplication.palette().base().color().name()
    win.theme_actions["dark"].trigger()
    assert is_dark() and win.ui.value("ui/theme") == "dark" and win.theme_actions["dark"].isChecked()
    assert type(win).__name__ and win.chart.axis_text == "#c8ccd2"
    win.set_theme("light")
    assert not is_dark() and win.chart.axis_text == "#404040"
    pal = QApplication.palette()   # white, even with Windows in dark mode
    assert pal.window().color().name() == pal.base().color().name() == "#ffffff"
    assert "#ffffff" in win.console.terminal.styleSheet()     # the console follows the theme
    win.set_theme("dark")
    assert "#0d1117" in win.console.terminal.styleSheet()
    win.set_theme("light")
    win.set_theme("system")
    assert QApplication.palette().base().color().name() == system and win.ui.value("ui/theme") == "system"


def test_drag_and_drop_to_tabs_and_chart(qtbot, win):
    from PySide6.QtCore import QEvent, QPoint, QPointF
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from rdt.gui.dnd import MIME, exprs_from
    sym = win.symbols.browser("main").list
    items = [sym.topLevelItem(i) for i in range(sym.topLevelItemCount())]
    gains = next(i for i in items if i.text(0) == "gains")
    data = sym.mimeData([gains])
    assert data.hasFormat(MIME) and exprs_from(data) == ["main:gains"]

    def drop(widget, mime):
        enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(widget, enter)
        ev = QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier, QEvent.Drop)
        QApplication.sendEvent(widget, ev)
        return ev.isAccepted()

    win.show()

    from PySide6.QtWidgets import QApplication
    tree = win.watch_tree
    assert drop(tree.viewport(), data) and "main:gains" in tree.states
    assert drop(tree.viewport(), data) and list(tree.states).count("main:gains") == 1   # not twice
    connect(qtbot, win)
    speed = sym.mimeData([next(i for i in items if i.text(0) == "tick_count")])
    drop(win.chart.plot.viewport(), speed)    # not watched in this tab: added, then charted after a read
    qtbot.waitUntil(lambda: "main:tick_count" in win.chart.series, timeout=5000)
    drop(win.chart.plot.viewport(), data)     # an array is not a number
    qtbot.waitUntil(lambda: not win.chart_pending, timeout=5000)
    assert "main:gains" not in win.chart.series


def test_mcu_cards(qtbot, win):
    """The Target panel: one card per MCU with its tag, link and ELF, one open at a time (GUI-14)."""
    main, motor = card(win, "main"), card(win, "motorctl")
    assert main.tag.text() == "MAIN" and motor.tag.text() == "MOTO"
    assert "not connected" in main.link.text() and "main.elf" in main.elf.text()
    win.symbols.toggle("motorctl")
    assert main.browser.isHidden() and not motor.browser.isHidden()
    win.symbols.toggle("motorctl")
    assert motor.browser.isHidden()
    main._toggle_ranges()
    names = [main.ranges.topLevelItem(i).text(0) for i in range(main.ranges.topLevelItemCount())]
    assert "GPIOA" in names and any(n.startswith(".") for n in names)   # a range and ELF sections
    connect(qtbot, win)
    qtbot.waitUntil(lambda: " ms" in main.link.text() or "answering" in main.link.text(), timeout=5000)
    st = win.device.target.session.mcu_status(1)
    st.timeouts.append(time.monotonic())
    motor._look = None
    win._show_state()
    assert "1 timeout" in motor.link.text() and "#d99a0b" in motor.dot.styleSheet()
    win.show_memory("motorctl")
    assert win.memory.mcu.currentText() == "motorctl"


def test_panel_buttons(qtbot, win):
    """Each panel button shows or hides its own panel, and the layout is kept (GUI-14)."""
    win.show()
    btn = win.modes.buttons
    qtbot.waitUntil(lambda: btn["scope"].isChecked() and not btn["console"].isChecked())   # console and memory are tabs behind the chart
    assert btn["watch"].isChecked()
    btn["scope"].click()                       # the chart is in front of the console and memory tabs: hides
    assert win.docks["Chart"].isHidden() and not btn["scope"].isChecked()
    assert win.docks["Target"].isVisible() and win.centralWidget().isVisible()
    btn["scope"].click()
    qtbot.waitUntil(lambda: not win.docks["Chart"].isHidden() and btn["scope"].isChecked())
    btn["watch"].click()
    assert win.centralWidget().isHidden() and not btn["watch"].isChecked()
    shortcut = next(a for a in win.findChildren(QAction) if a.shortcut().toString() == "Ctrl+4")
    shortcut.trigger()                         # Console is behind the chart: comes to the front
    qtbot.waitUntil(lambda: btn["console"].isChecked() and not win.docks["Console"].visibleRegion().isEmpty())
    shortcut.trigger()
    qtbot.waitUntil(lambda: win.docks["Console"].isHidden() and not btn["console"].isChecked())
    assert win.ui.value("session/layout") and win.ui.value("session/watch_shown") is False
    win.reset_layout()
    assert win.centralWidget().isVisible() and btn["watch"].isChecked() and btn["scope"].isChecked()


# --- look and feel (LNF-1..8) --------------------------------------------------------------------

def test_new_themes_and_density(qtbot, win):
    """Studio dark, Islands, Instrument and Light pro (LNF-1); compact or comfortable rows (LNF-6)."""
    from PySide6.QtWidgets import QApplication
    from rdt.gui.style import THEMES, is_dark
    assert {"studio", "islands", "instrument", "light_pro"} <= set(THEMES)
    win.show()
    win.theme_actions["studio"].trigger()
    assert win.ui.value("ui/theme") == "studio" and is_dark() and QApplication.instance().styleSheet()
    assert win.toolBarArea(win.modes) == Qt.LeftToolBarArea          # the panel buttons as a strip at the left
    assert win.modes.buttons["watch"].toolButtonStyle() == Qt.ToolButtonIconOnly
    assert win.chart.axis_text == "#a0a0a0"
    win.set_theme("light_pro")
    assert not is_dark() and win.toolBarArea(win.modes) == Qt.TopToolBarArea
    assert win.menuWidget().palette().window().color().name() == "#03234b"   # the navy title bar
    win.set_theme("instrument")
    assert win.chart.plot.backgroundBrush().color().name() == "#000000"
    win.density_actions["comfortable"].trigger()
    assert win.ui.value("ui/density") == "comfortable" and "QTreeView::item" in QApplication.instance().styleSheet()
    win.set_density("compact")
    assert "QTreeView::item" not in QApplication.instance().styleSheet()
    win.set_theme("system")
    assert win.menuWidget().palette().window().color() == QApplication.palette().window().color()
    assert not QApplication.instance().styleSheet()


def test_run_stop_and_icon_buttons(qtbot, win):
    """Polling is a RUN/STOP key (LNF-2); Poll now, Log CSV and the profile are icon buttons with
    tooltips (LNF-3)."""
    bar = win.bar
    assert "RUN" in bar.polling.text() and bar.polling.isChecked()
    bar.polling.click()
    assert "STOP" in bar.polling.text() and not win.watch_tabs.polling
    win.polling_action.trigger()                      # F7 lights RUN again
    assert "RUN" in bar.polling.text() and bar.polling.isChecked()
    for b in (bar.poll_now, bar.log_button, bar.profile):
        assert b.text() == "" and not b.icon().isNull() and b.toolTip()
    assert "RDT simulator" in bar.profile.toolTip()


def test_trace_takes_its_mcu_colour(qtbot, win):
    """A chart trace is drawn in its MCU's colour, a lighter or darker turn for the next (LNF-4)."""
    from rdt.target import mcu_color
    connect(qtbot, win)
    tree = win.watch_tree
    qtbot.waitUntil(lambda: bool(tree.chartable("main:motor.speed") and tree.chartable("main:motor.target")),
                    timeout=5000)
    tree.chart("main:motor.speed")
    tree.chart("main:motor.target")
    main = mcu_color(next(m for m in win.ws.config.mcus if m.name == "main"))
    first, second = win.chart.series["main:motor.speed"].color, win.chart.series["main:motor.target"].color
    assert first == main and second != main


def test_link_strip_in_status_bar(qtbot, win):
    """The status bar is a strip in the link's colour with each MCU's reply time (LNF-5)."""
    connect(qtbot, win)
    qtbot.waitUntil(lambda: "main" in win.link_label.text() and " ms" in win.link_label.text(), timeout=5000)
    assert win.link_label.text().startswith("Connected") and "#1f7a46" in win.statusBar().styleSheet()
    win.disconnect()
    qtbot.waitUntil(lambda: win.link_label.text().startswith("Disconnected"), timeout=5000)
    assert win.statusBar().styleSheet() == ""


def test_row_hover_buttons_and_mono_values(qtbot, win):
    """A watched row shows Chart and Remove buttons under the mouse (LNF-7); values are in a
    fixed-width font (LNF-8)."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QFontInfo, QMouseEvent
    from PySide6.QtWidgets import QStyleOptionViewItem
    from rdt.gui.variables import COL_NAME
    connect(qtbot, win)
    tree = win.watch_tree
    qtbot.waitUntil(lambda: tree.chartable("main:motor.speed") is not None, timeout=5000)
    idx = tree.indexFromItem(tree.states["main:motor.speed"].item, COL_NAME)
    delegate = tree.itemDelegateForColumn(COL_NAME)
    opt = QStyleOptionViewItem()
    opt.rect = tree.visualRect(idx)
    buttons = dict(delegate.buttons(opt.rect, idx))
    assert set(buttons) == {"chart", "remove"}

    def click(kind):
        pos = QPointF(buttons[kind].center())
        for t in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            ev = QMouseEvent(t, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            assert delegate.editorEvent(ev, tree.model(), opt, idx)

    click("chart")
    qtbot.waitUntil(lambda: win.chart.has("main:motor.speed"))
    buttons = dict(delegate.buttons(opt.rect, idx))
    assert set(buttons) == {"remove"}                 # already charted
    click("remove")
    qtbot.waitUntil(lambda: "main:motor.speed" not in tree.states)
    value = tree.itemDelegateForColumn(1)
    vopt = QStyleOptionViewItem()
    value.initStyleOption(vopt, tree.model().index(0, 1))
    assert QFontInfo(vopt.font).fixedPitch()
