"""GUI tests: the main window against the simulated device, run offscreen with pytest-qt."""

import os
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
from rdt.gui.variables import COL_VALUE  # noqa: E402
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
    for st in win.watch_tree.states.values():
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
    assert "connected" in win.bar.state.text()
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


def test_console_buttons_and_memory(qtbot, win, monkeypatch):
    connect(qtbot, win)
    con = win.console
    con.input.setText("*IDN?")
    con.input.returnPressed.emit()
    qtbot.waitUntil(lambda: "SIM" in con.output.toPlainText().upper(), timeout=3000)
    assert con.history.items == ["*IDN?"]
    con.send("BOGUS:COMMAND?")
    qtbot.waitUntil(lambda: "(no reply" in con.output.toPlainText(), timeout=3000)

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("1", True))
    silence = next(b for b in win.ws.buttons if b.name == "Silence MCU")
    win.buttons.press(silence)
    qtbot.waitUntil(lambda: "> SIM:MCU:SIL 1,ON" in con.output.toPlainText(), timeout=3000)
    win.buttons.press(next(b for b in win.ws.buttons if b.name == "Identify"))

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


def test_profile_dialog_preview_and_apply(qtbot, win):
    dlg = ProfileDialog(win.ws.config.profile, win.device, win)
    qtbot.addWidget(dlg)
    assert "MEMory:READ? 0,0x20000000,16" in dlg.preview.text()
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
    dlg.table.item(0, 1).setText("motorctl")
    with pytest.raises(Exception, match="unique"):
        dlg.result_mcus()


def test_elf_rebuild_remaps(qtbot, win, tmp_path):
    elf = tmp_path / "main.elf"
    shutil.copy(win.ws.config.mcus[0].elf, elf)
    win.ws.config.mcus[0].elf = elf
    connect(qtbot, win)
    wait_value(qtbot, win, "main:tick_count", lambda v: True)
    st = win.watch_tree.states["main:tick_count"]
    rebuilt = ROOT / "tests" / "elf" / "corpus-clang-v2.elf"
    if not rebuilt.exists():
        pytest.skip("no rebuild variant ELF")
    shutil.copy(rebuilt, elf)
    os.utime(elf, (time.time() - 5,) * 2)   # settled: the linker is done with it
    win._check_elfs()
    qtbot.waitUntil(lambda: st.gone or st.note is not None, timeout=5000)


def test_link_down_greys_panels(qtbot, win, sim):
    connect(qtbot, win)
    assert win.console.isEnabled()
    win.disconnect()
    qtbot.waitUntil(lambda: win.bar.connect_button.text() == "Connect", timeout=3000)
    assert not win.console.isEnabled()
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
