"""RDT main window: connection bar, watch tabs, variable browser, chart, memory view and SCPI console,
all working on one workspace (``.rdtw``).

    rdt-gui [bench.rdtw]
    python -m rdt.gui [bench.rdtw]
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QToolBar,
    QVBoxLayout,
)

from ..console import history_path
from ..csvlog import CsvLog
from ..errors import ConfigError, LinkDown
from ..profile import Profile
from ..session import LinkState
from ..target import ConnectionConfig, McuConfig, TargetConfig
from ..workspace import Workspace
from .chart import ChartPanel
from .device import Device, describe_error
from .panels import CONSOLE_KEYS, Console, McuDialog, MemoryView, ProfileDialog
from .variables import SymbolBrowser, WatchTabs, WatchTree

FILTER = "RDT workspace (*.rdtw);;Target file (*.toml);;All files (*)"
TICK_MS = 50            # how often the watch tabs look for items that are due
ELF_CHECK_MS = 1000     # how often to look for rebuilt ELF files
BAUDS = ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600")


def new_workspace() -> Workspace:
    return Workspace(TargetConfig(mcus=[McuConfig(0, "main")]))


def serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(p.device for p in list_ports.comports())


class ConnectionBar(QToolBar):
    """Transport, address (IP and port, or COM port and baud), SCPI profile, and connect."""

    def __init__(self, main):
        super().__init__("Connection")
        self.main = main
        self.setObjectName("connection")
        self.transport = QComboBox()
        self.transport.addItems(["tcp", "udp", "serial"])
        self.transport.setToolTip("TCP or UDP socket, or a COM port")
        self.host = QLineEdit()
        self.host.setPlaceholderText("IP address")
        self.host.setMaximumWidth(160)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.com = QComboBox(editable=True)
        self.com.setMinimumWidth(110)
        self.baud = QComboBox(editable=True)
        self.baud.addItems(BAUDS)
        self.profile = QPushButton()
        self.profile.setToolTip("Edit the SCPI profile: command templates and reply format")
        self.profile.clicked.connect(main.edit_profile)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(main.toggle_connection)
        self.state = QLabel()
        self.state.setMinimumWidth(180)
        self.poll = QSpinBox()
        self.poll.setRange(0, 3_600_000)
        self.poll.setSingleStep(100)
        self.poll.setSuffix(" ms")
        self.poll.setSpecialValueText("Off")
        self.poll.setToolTip("Global polling interval; each watched item can override it")
        self.poll.valueChanged.connect(main.set_poll_ms)
        self.polling = QCheckBox("Poll")
        self.polling.setChecked(True)
        self.polling.setToolTip("Poll the watched items; untick to stop all polling, "
                                "including items with their own interval (F7)")
        self.polling.toggled.connect(main.set_polling)
        self.poll_now = QPushButton("Poll now")
        self.poll_now.setToolTip("Read every watched item once, in every tab (F6)")
        self.poll_now.clicked.connect(main.read_all)
        self.log_button = QPushButton("Log CSV")
        self.log_button.setCheckable(True)
        self.log_button.setToolTip("Log every watched value to a CSV file while connected (Ctrl+L)")
        self.log_button.clicked.connect(main.toggle_log)
        self.addWidget(self.transport)
        self._lan = [self.addWidget(self.host), self.addWidget(QLabel(":")), self.addWidget(self.port)]
        self._serial = [self.addWidget(self.com), self.addWidget(self.baud)]
        self.addSeparator()
        self.addWidget(self.profile)
        self.addWidget(self.connect_button)
        self.addWidget(self.state)
        self.addSeparator()
        self.addWidget(self.polling)
        self.addWidget(self.poll)
        self.addWidget(self.poll_now)
        self.addWidget(self.log_button)
        self.transport.currentTextChanged.connect(self._kind)
        for w in (self.transport, self.com, self.baud):
            w.currentTextChanged.connect(main.connection_edited)
        self.host.textEdited.connect(main.connection_edited)
        self.port.valueChanged.connect(main.connection_edited)

    def _kind(self, kind: str) -> None:
        for a in self._lan:
            a.setVisible(kind != "serial")
        for a in self._serial:
            a.setVisible(kind == "serial")
        if kind == "serial" and self.com.count() == 0:
            self.com.addItems(serial_ports())

    def show_config(self, ws: Workspace) -> None:
        widgets = (self.transport, self.host, self.port, self.com, self.baud, self.poll)
        for w in widgets:
            w.blockSignals(True)
        c = ws.config.connection
        self.transport.setCurrentText(c.transport)
        if c.transport == "serial":
            self.com.setCurrentText(c.address)
            self.baud.setCurrentText(str(c.baud))
        else:
            host, _, port = c.address.rpartition(":")
            self.host.setText(host or c.address)
            self.port.setValue(int(port) if port.isdigit() else 5025)
        self.poll.setValue(ws.poll_ms)
        for w in widgets:
            w.blockSignals(False)
        self._kind(c.transport)
        self.show_profile(ws)

    def show_profile(self, ws: Workspace) -> None:
        cfg = ws.config
        where = f" ({cfg.profile_path.name})" if cfg.profile_path else ""
        self.profile.setText(f"Profile: {cfg.profile.name}{where}")

    def connection(self) -> ConnectionConfig:
        kind = self.transport.currentText()
        if kind == "serial":
            port = self.com.currentText().strip()
            if not port:
                raise ConfigError("choose a COM port")
            try:
                baud = int(self.baud.currentText())
            except ValueError:
                raise ConfigError(f"baud rate {self.baud.currentText()!r} is not a number") from None
            return ConnectionConfig("serial", port, baud)
        host = self.host.text().strip()
        if not host:
            raise ConfigError("type the device's IP address")
        return ConnectionConfig(kind, f"{host}:{self.port.value()}")


class MainWindow(QMainWindow):
    def __init__(self, ws: Workspace | None = None):
        super().__init__()
        self.ws = ws or new_workspace()
        self.dirty = False
        self.device = Device(self)
        self.device.opened.connect(self._opened)
        self.device.closed.connect(self._show_state)
        self._connecting = False
        self._last_state: str | None = None
        self.log: CsvLog | None = None
        self._log_link_up = True

        self.chart = ChartPanel(self)
        self.chart.changed.connect(self._chart_changed)
        self.chart.series_changed.connect(self._chart_series_changed)
        self.watch_tabs = WatchTabs(self)
        self.watch_tabs.changed.connect(self.set_dirty)
        self.watch_tabs.sampled.connect(self.chart.feed)
        self.watch_tabs.sampled.connect(self._log_readings)
        self.watch_tabs.chart_requested.connect(self.add_to_chart)
        self.setCentralWidget(self.watch_tabs)
        self.symbols = SymbolBrowser(self)
        self.symbols.add_requested.connect(lambda expr: self.watch_tabs.add_watch(expr))
        self.console = Console(self)
        self.memory = MemoryView(self)
        self.bar = ConnectionBar(self)
        self.addToolBar(self.bar)
        self.docks = {}
        for title, widget, area in (("Variables", self.symbols, Qt.LeftDockWidgetArea),
                                    ("Chart", self.chart, Qt.BottomDockWidgetArea),
                                    ("Console", self.console, Qt.BottomDockWidgetArea),
                                    ("Memory", self.memory, Qt.BottomDockWidgetArea)):
            dock = QDockWidget(title, self)
            dock.setObjectName(title.lower())
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            self.docks[title] = dock
        self.tabifyDockWidget(self.docks["Chart"], self.docks["Console"])
        self.tabifyDockWidget(self.docks["Console"], self.docks["Memory"])
        self.docks["Chart"].raise_()
        self.resizeDocks([self.docks["Chart"]], [300], Qt.Vertical)
        self.resizeDocks([self.docks["Variables"]], [250], Qt.Horizontal)
        self._menus()
        self.message = QLabel()
        self.statusBar().addWidget(self.message, 1)
        self.log_label = QLabel()
        self.statusBar().addPermanentWidget(self.log_label)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.watch_tabs.tick)
        self.poll_timer.start(TICK_MS)
        self.state_timer = QTimer(self)
        self.state_timer.timeout.connect(self._show_state)
        self.state_timer.start(250)
        self.elf_timer = QTimer(self)
        self.elf_timer.timeout.connect(self._check_elfs)
        self.elf_timer.start(ELF_CHECK_MS)
        self.resize(1200, 800)
        self.show_workspace()

    # --- menus -------------------------------------------------------------------------------

    def _menus(self) -> None:
        def action(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            menu.addAction(a)
            return a

        m = self.menuBar().addMenu("&File")
        action(m, "&New workspace", self.new_file, QKeySequence.New)
        action(m, "&Open...", self.open_file, QKeySequence.Open)
        action(m, "&Save", self.save, QKeySequence.Save)
        action(m, "Save &as...", self.save_as, QKeySequence.SaveAs)
        m.addSeparator()
        action(m, "&Quit", self.close, QKeySequence.Quit)
        m = self.menuBar().addMenu("&Target")
        action(m, "&Connect or disconnect", self.toggle_connection, "F5")
        action(m, "SCPI &profile...", self.edit_profile)
        action(m, "Load profile file...", self.load_profile)
        action(m, "&MCUs and ELF files...", self.edit_mcus)
        m.addSeparator()
        action(m, "Check ELF &identity", lambda: self.check_identity())
        self.allow_action = action(m, "Allow writes despite ELF &mismatch", self.allow_mismatch)
        self.allow_action.setEnabled(False)
        m = self.menuBar().addMenu("&Watch")
        action(m, "&Add expression...", self.add_expression, "Ctrl+E")
        action(m, "&Poll now", self.read_all, "F6")
        self.polling_action = action(m, "Poll &automatically", lambda: self.set_polling(self.polling_action.isChecked()), "F7")
        self.polling_action.setCheckable(True)
        self.polling_action.setChecked(True)
        action(m, "Re&move selected", lambda: [self.watch_tree.remove_watch(e) for e in self.watch_tree.selected_exprs()],
               QKeySequence.Delete)
        m.addSeparator()
        action(m, "&New tab...", lambda: self.watch_tabs.new_tab(), "Ctrl+Shift+T")
        action(m, "Re&name tab...", lambda: self.watch_tabs.rename_tab())
        action(m, "Remove &tab", lambda: self.watch_tabs.close_tab())
        action(m, "Next tab", lambda: self.watch_tabs.setCurrentIndex(
            (self.watch_tabs.currentIndex() + 1) % self.watch_tabs.count()), "Ctrl+PgDown")
        action(m, "Previous tab", lambda: self.watch_tabs.setCurrentIndex(
            (self.watch_tabs.currentIndex() - 1) % self.watch_tabs.count()), "Ctrl+PgUp")
        m.addSeparator()
        action(m, "Add selected to &chart", self.chart_selected, "Ctrl+G")
        action(m, "Reset &statistics", lambda: self.watch_tabs.reset_stats())
        self.log_action = action(m, "&Log to CSV...", self.toggle_log, "Ctrl+L")
        self.log_action.setCheckable(True)
        self.read_only_action = action(m, "All items &read-only", self.set_read_only)
        self.read_only_action.setCheckable(True)
        m = self.menuBar().addMenu("&View")
        for dock in self.docks.values():
            m.addAction(dock.toggleViewAction())
        m.addSeparator()
        stats = action(m, "&Statistics columns", self.watch_tabs.show_stats, "Ctrl+T")
        stats.setCheckable(True)
        m = self.menuBar().addMenu("&Help")
        action(m, "&Keyboard shortcuts", self.show_shortcuts, "F1")

    def shortcut_rows(self) -> list[tuple[str, str, str]]:
        """(where, what, keys) for every menu shortcut, read from the menus themselves, then the
        console's keys."""
        rows = []
        for top in self.menuBar().actions():
            menu = top.menu()
            for a in menu.actions() if menu else []:
                if not a.shortcut().isEmpty():
                    rows.append((top.text().replace("&", ""), a.text().replace("&", "").rstrip("."),
                                 a.shortcut().toString(QKeySequence.NativeText)))
        rows += [("Console", what, keys) for keys, what in CONSOLE_KEYS]
        return rows

    def show_shortcuts(self) -> QDialog:
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard shortcuts")
        dlg.resize(560, 520)
        layout = QVBoxLayout(dlg)
        rows = self.shortcut_rows()
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["Where", "Action", "Keys"])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(text))
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        return dlg

    # --- workspace ---------------------------------------------------------------------------

    def show_workspace(self) -> None:
        self.bar.show_config(self.ws)
        self.watch_tabs.rebuild()
        self.chart.load(self.ws.chart)
        self.symbols.set_workspace(self.ws)
        self.memory.set_workspace(self.ws)
        self.console.set_workspace(self.ws)
        self.read_only_action.setChecked(self.ws.read_only)
        self.set_dirty(False)

    @property
    def watch_tree(self) -> WatchTree:
        """The watch tree of the tab in front."""
        return self.watch_tabs.current()

    def set_dirty(self, dirty: bool = True) -> None:
        self.dirty = dirty
        name = self.ws.path.name if self.ws.path else "untitled"
        self.setWindowTitle(f"{name}{' *' if dirty else ''} - RDT")

    def status(self, msg: str, error: bool = False) -> None:
        self.message.setText(msg)
        self.message.setStyleSheet("color: #c03030" if error else "")

    def _maybe_save(self) -> bool:
        """Ask to save unsaved changes. False when the user cancels."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(self, "RDT", "Save changes to the workspace?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Cancel:
            return False
        return answer == QMessageBox.Discard or self.save()

    def new_file(self) -> None:
        if self._maybe_save():
            self.load(new_workspace())

    def open_file(self) -> None:
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open workspace", "", FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str | Path) -> bool:
        try:
            ws = Workspace.load(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Open workspace", str(e))
            return False
        if Path(path).suffix.lower() != ".rdtw":
            ws.path = None   # a target file opens as a new workspace; don't overwrite it on save
        self.load(ws)
        return True

    @staticmethod
    def settings() -> QSettings:
        """Per-user GUI settings, next to the console history."""
        return QSettings(str(history_path().parent / "gui.ini"), QSettings.IniFormat)

    def remember(self, path: Path | None) -> None:
        """Remember the workspace file, to reopen it on the next start (WSP-3)."""
        if path is not None:
            self.settings().setValue("last_workspace", str(Path(path).resolve()))

    def reopen_last(self) -> bool:
        """Open the workspace that was open last time, if its file is still there."""
        last = self.settings().value("last_workspace", "")
        if not last:
            return False
        if not Path(last).is_file():
            self.status(f"The last workspace {last} is gone; starting with a new one")
            return False
        return self.open_path(last)

    def load(self, ws: Workspace) -> None:
        self.stop_log()
        if self.device.target is not None:
            self.device.close()
        self.ws = ws
        self.remember(ws.path)
        self.show_workspace()
        msg = f"Opened {ws.path}" if ws.path else "New workspace"
        self.status("; ".join([msg] + ws.warnings))

    def save(self) -> bool:
        if self.ws.path is None:
            return self.save_as()
        return self._save_to(self.ws.path)

    def save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save workspace", str(self.ws.path or "bench.rdtw"),
                                              "RDT workspace (*.rdtw)")
        if not path:
            return False
        if not path.lower().endswith(".rdtw"):
            path += ".rdtw"
        return self._save_to(Path(path))

    def _save_to(self, path: Path) -> bool:
        try:
            self._take_connection()
            self.ws.save(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Save workspace", str(e))
            return False
        self.set_dirty(False)
        self.remember(path)
        self.status(f"Saved {path}")
        return True

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        for t in (self.poll_timer, self.state_timer, self.elf_timer, self.memory.timer, self.chart.timer):
            t.stop()
        self.stop_log()
        self.console.stop_log()
        self.device.shutdown()
        event.accept()

    # --- connection --------------------------------------------------------------------------

    def connection_edited(self, *_) -> None:
        self.set_dirty()

    def _take_connection(self) -> None:
        self.ws.config.connection = self.bar.connection()

    def set_read_only(self, on: bool) -> None:
        """The workspace's read-only default (WS-4): every item refuses writes unless its own
        settings allow them."""
        if on != self.ws.read_only:
            self.ws.read_only = on
            self.set_dirty()
            self.watch_tabs.decorate()
            self.status("Every item is read-only unless its settings allow writes" if on
                        else "Items are writable unless their settings make them read-only")

    def set_poll_ms(self, ms: int) -> None:
        if ms != self.ws.poll_ms:
            self.ws.poll_ms = ms
            self.set_dirty()
            self.watch_tabs.decorate()
            if self.memory.auto.isChecked():
                self.memory._auto(True)

    def set_polling(self, on: bool) -> None:
        """Turn all polling on or off; Poll now (F6) still reads every item once."""
        if on == self.watch_tabs.polling:
            return
        self.watch_tabs.polling = on
        for w in (self.bar.polling, self.polling_action):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.bar.poll.setEnabled(on)
        self.status("Polling on" if on else "Polling off: press Poll now (F6) to read the watched items")

    def toggle_connection(self) -> None:
        if self.device.target is not None or self._connecting:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        try:
            self._take_connection()
        except ConfigError as e:
            self.status(str(e), error=True)
            return
        self._connecting = True
        self.status(f"Connecting to {self.ws.config.connection.describe()}...")
        self._show_state()
        self.device.open(self.ws.config, self._open_failed)

    def disconnect(self) -> None:
        self._connecting = False
        self.device.close(lambda _: self.status("Disconnected"))
        self.allow_action.setEnabled(False)
        self._show_state()

    def reconnect(self) -> None:
        if self.device.target is not None or self._connecting:
            self.connect()

    def _opened(self, t) -> None:
        self._connecting = False
        self.status(f"Connected to {t.idn or self.ws.config.connection.describe()}")
        self.show_identity([m.identity for m in t.mcus.values() if m.identity is not None])
        self.read_all()
        self._show_state()

    # --- ELF identity (SYM-8) ------------------------------------------------------------------

    def show_identity(self, results) -> None:
        """Note each MCU's identity check in the console; a mismatch also goes to the status bar
        and enables Target > Allow writes despite ELF mismatch."""
        for r in results:
            if r.status != "unchecked":
                self.console.log(str(r), "error" if r.status == "mismatch" else "note")
        bad = [r for r in results if r.status == "mismatch"]
        if bad:
            self.status("ELF does not match the firmware on " + ", ".join(r.mcu for r in bad) +
                        "; writes to it are refused (Target > Allow writes despite ELF mismatch)", error=True)
        t = self.device.target
        self.allow_action.setEnabled(t is not None and any(
            m.identity is not None and m.identity.status == "mismatch" and not m.allow_mismatch
            for m in t.mcus.values()))

    def check_identity(self, mcus: list[str] | None = None) -> None:
        """Compare the ELFs with the firmware again, such as after flashing."""
        if self.device.target is None:
            self.status("Connect first to check the ELF identity", error=True)
            return

        def done(results):
            self.show_identity(results)
            if not any(r.status == "mismatch" for r in results):
                self.status("ELF identity: " + "; ".join(f"{r.mcu} {r.status}" for r in results))

        self.device.submit(lambda t: [r for name in (mcus or list(t.mcus)) for r in t.check_identity(name)],
                           done, lambda e: self.status(describe_error(e), True))

    def allow_mismatch(self) -> None:
        def allow(t):
            names = [m.name for m in t.mcus.values() if m.identity is not None and m.identity.status == "mismatch"]
            for n in names:
                t.allow_mismatch(n)
            return names

        def done(names):
            self.allow_action.setEnabled(False)
            if names:
                self.status(f"Writes allowed to {', '.join(names)} although its ELF does not match")
                self.console.log(f"writes allowed despite ELF mismatch: {', '.join(names)}", "note")

        self.device.submit(allow, done)

    def _open_failed(self, e: Exception) -> None:
        self._connecting = False
        self.status(f"Cannot connect: {describe_error(e)}", error=True)
        self._show_state()

    def _show_state(self) -> None:
        state = self.device.state
        if self._connecting:
            text, color, button = "connecting...", "#c87800", "Cancel"
        elif state == LinkState.UP:
            idn = self.device.target.idn if self.device.target else None
            text, color, button = "● connected" + (f": {idn[:40]}" if idn else ""), "#208020", "Disconnect"
        elif state == LinkState.DOWN:
            text, color, button = "● link down, reconnecting", "#c03030", "Disconnect"
        else:
            text, color, button = "○ not connected", "#888888", "Connect"
        if text != self._last_state:
            if self._last_state is not None:
                self.console.log(text.lstrip("●○ "), "error" if state == LinkState.DOWN else "note")
            self._last_state = text
        self.bar.state.setText(text)
        self.bar.state.setStyleSheet(f"color: {color}")
        self.bar.connect_button.setText(button)
        up = state == LinkState.UP
        self.memory.setEnabled(up)
        self.console.set_link_up(up)
        for w in (self.bar.transport, self.bar.host, self.bar.port, self.bar.com, self.bar.baud):
            w.setEnabled(self.device.target is None and not self._connecting)

    def _check_elfs(self) -> None:
        if self.device.target is None or self.device.busy("elfs"):
            return

        def reloaded(mcus):
            if mcus and self.log is not None:
                self.log.event(time.time(), f"ELF rebuilt for {', '.join(mcus)}")
            if mcus:
                self.status(f"ELF rebuilt for {', '.join(mcus)}; watch items found again by name")
                self.watch_tabs.remap(mcus)
                self.symbols.reload()
                self.check_identity(mcus)

        self.device.submit(lambda t: t.refresh_elfs(), reloaded, lambda e: self.status(describe_error(e), True),
                           key="elfs")

    # --- target settings ---------------------------------------------------------------------

    def edit_profile(self) -> None:
        dlg = ProfileDialog(self.ws.config.profile, self.device, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.apply_profile(dlg.result_profile())

    def apply_profile(self, profile: Profile, path: Path | None = None) -> None:
        self.ws.config.profile = profile
        self.ws.config.profile_path = path   # an edited profile is saved inside the workspace
        self.bar.show_profile(self.ws)
        self.set_dirty()
        self.reconnect()

    def load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load SCPI profile", "", "Profile (*.toml);;All files (*)")
        if not path:
            return
        try:
            self.apply_profile(Profile.load(path), Path(path))
        except ConfigError as e:
            QMessageBox.warning(self, "Load profile", str(e))

    def edit_mcus(self) -> None:
        dlg = McuDialog(self.ws.config.mcus, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.ws.config.mcus = dlg.result_mcus()
        self.symbols.set_workspace(self.ws)
        self.memory.set_workspace(self.ws)
        self.set_dirty()
        self.reconnect()

    # --- watch -------------------------------------------------------------------------------

    def add_expression(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        mcu = self.ws.config.mcus[0].name if self.ws.config.mcus else "main"
        text, ok = QInputDialog.getText(self, "Add to watch list",
                                        f"Expression, such as {mcu}:motor.speed or @{mcu}:0x20000000:u32")
        if ok and text.strip():
            self.watch_tree.add_watch(text.strip())

    # --- chart -------------------------------------------------------------------------------

    def reads_closed(self, expr: str) -> bool:
        """True when a collapsed struct or array must be read anyway: it has a charted field, or
        everything is being logged."""
        return self.log is not None or self.chart.wants(expr)

    def add_to_chart(self, path: str, unit: str = "") -> None:
        self.chart.add(path, unit)
        self.docks["Chart"].show()
        self.docks["Chart"].raise_()

    def chart_selected(self) -> None:
        paths = [p for p in self.watch_tree.selected_paths() if self.watch_tree.chartable(p)]
        if not paths:
            self.status("Select numeric values in the watch tree to chart them")
        for p in paths:
            self.watch_tree.chart(p)

    def _chart_changed(self) -> None:
        self.ws.chart = self.chart.to_dict()
        self.set_dirty()

    def _chart_series_changed(self) -> None:
        for expr in self.watch_tabs.exprs():
            if self.chart.wants(expr):
                self.watch_tabs.force(expr)

    # --- CSV log -----------------------------------------------------------------------------

    def toggle_log(self) -> None:
        if self.log is not None:
            self.stop_log()
        else:
            self.start_log()
        self._show_log()

    def start_log(self, path: str | Path | None = None) -> bool:
        if self.device.target is None:
            self.status("Connect first: the log's columns come from the values read", error=True)
            self._show_log()
            return False
        if path is None:
            base = self.ws.path.parent if self.ws.path else Path.cwd()
            name = base / f"rdt-{datetime.now():%Y%m%d-%H%M%S}.csv"
            path, _ = QFileDialog.getSaveFileName(self, "Log to CSV", str(name), "CSV (*.csv)")
            if not path:
                self._show_log()
                return False
        columns, units = [], {}
        for _, st in self.watch_tabs.states():
            expr = st.watch.expr
            leaves = [p for p, r in st.shown.items() if r.text is not None and r.raw is not None] or [expr]
            columns += [p for p in leaves if p not in columns]
            if st.watch.display.unit:
                units[expr] = st.watch.display.unit
        try:
            self.log = CsvLog(path, columns, units)
        except ConfigError as e:
            self.status(str(e), error=True)
            self._show_log()
            return False
        self._log_link_up = True
        self.watch_tabs.force()   # a first row with everything
        self.status(f"Logging {len(columns)} values to {path}")
        self._show_log()
        return True

    def stop_log(self) -> None:
        if self.log is None:
            return
        log, self.log = self.log, None
        log.close()
        self.status(f"Logged {log.rows} rows to {log.path}")
        self._show_log()

    def _log_readings(self, readings) -> None:
        values = {}
        for r in readings:
            for row in r.shown or ():
                if row.text is not None and row.raw is not None:
                    values[row.path] = row.value
        was_up = self._log_link_up
        if any(isinstance(r.error, LinkDown) for r in readings):
            self._log_link_up = False
        elif values:
            self._log_link_up = True
        if self.log is None:
            return
        event = "" if was_up == self._log_link_up else ("link up" if self._log_link_up else "link down")
        if values or event:
            when = min((r.time for r in readings if r.time), default=time.time())
            self.log.write(when, values, event)
        self._show_log()

    def _show_log(self) -> None:
        on = self.log is not None
        for w in (self.bar.log_button, self.log_action):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.bar.log_button.setText("● Logging" if on else "Log CSV")
        self.bar.log_button.setStyleSheet("color: #c03030" if on else "")
        self.log_label.setText(f"CSV: {self.log.path.name}, {self.log.rows} rows" if on else "")

    def read_all(self) -> None:
        self.watch_tabs.read_all()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.WARNING)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("RDT")
    app.setOrganizationName("RDT")
    win = MainWindow()
    if argv:
        win.open_path(argv[0])
    elif not win.reopen_last() and Path("rdt.toml").is_file():
        win.open_path("rdt.toml")         # the CLI's default target file, as a starting point
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
