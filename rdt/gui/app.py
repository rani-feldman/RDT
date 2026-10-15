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

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QToolBar,
    QButtonGroup, QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from ..console import history_path
from ..csvlog import CsvLog
from ..errors import ConfigError, LinkDown
from ..profile import Profile
from ..session import LinkState
from ..target import ConnectionConfig, McuConfig, TargetConfig
from ..ui_settings import SETTINGS, UiSettings, workspace_dir
from ..workspace import Workspace, default_target
from .chart import ChartPanel
from .cards import McuCards
from .device import Device, describe_error
from .dnd import DropTarget
from .panels import CONSOLE_KEYS, Console, McuDialog, MemoryView, ProfileDialog, mono
from . import titlebar
from .style import THEMES, apply_theme, is_dark
from .variables import WatchTabs, WatchTree

FILTER = "RDT workspace (*.rdtw);;Target file (*.toml);;All files (*)"
WATCH_FILTER = "Watch list (*.watch.toml *.toml);;RDT workspace (*.rdtw);;All files (*)"
UI_FILTER = "Interface settings (*.toml);;All files (*)"
TICK_MS = 50            # how often the watch tabs look for items that are due
ELF_CHECK_MS = 1000     # how often to look for rebuilt ELF files
BAUDS = ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600")


def new_workspace() -> Workspace:
    return Workspace(TargetConfig(mcus=[McuConfig(0, "main")]))


def workspaces_folder() -> Path:
    """The folder the Open and Save dialogs start in: %LOCALAPPDATA%\\rdt, beside ui.toml."""
    folder = workspace_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def load_ui() -> UiSettings:
    """The per-user interface settings (ui.toml). The first time, they are taken from the INI
    files older versions kept them in (gui.ini and console.ini), which are then left alone."""
    ui = UiSettings.load()
    if ui.exists():
        return ui
    folder = history_path().parent
    old = {}
    for name, keys in (("gui.ini", {"last_workspace": "session/last_workspace", "memory/kind": "memory/kind",
                                    "memory/type": "memory/type", "memory/decimal": "memory/decimal"}),
                       ("console.ini", {"font_size": "console/font_size", "wrap": "console/wrap"})):
        if not (folder / name).is_file():
            continue
        ini = QSettings(str(folder / name), QSettings.IniFormat)
        for ini_key, key in keys.items():
            v = ini.value(ini_key)
            if v in (None, ""):
                continue
            kind = SETTINGS.get(key, str)
            try:
                old[key] = (str(v).lower() == "true") if kind is bool else kind(v)
            except (TypeError, ValueError):
                pass
    if old:
        ui.values = old
        ui.save()
    return ui


def serial_ports() -> list[tuple[str, str]]:
    """The COM ports present, as (port, description) such as ("COM3", "GAC serial port (COM3)")."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    found = []
    for p in list_ports.comports():
        desc = (p.description or "").strip()
        found.append((p.device, "" if desc in ("", "n/a", p.device) else desc))
    return sorted(found)


def port_label(port: str, description: str) -> str:
    """How a port shows in the list: ``COM3 - GAC serial port (COM3)``."""
    return f"{port} - {description}" if description else port


def port_name(text: str) -> str:
    """The port in what the COM box shows: a list entry's label, or a port typed by hand."""
    return text.split(" - ", 1)[0].strip()


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
        # One button shows the link state and connects or disconnects when clicked (GUI-11).
        self.connect_button = QPushButton("Disconnected")
        self.connect_button.setFont(mono())
        self.connect_button.clicked.connect(main.toggle_connection)
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
            typed = self.com.currentText()
            self.com.blockSignals(True)
            for port, desc in serial_ports():
                self.com.addItem(port_label(port, desc), port)
            self.com.view().setMinimumWidth(self.com.view().sizeHintForColumn(0) + 24)
            self.set_port(typed)
            self.com.blockSignals(False)

    def set_port(self, port: str) -> None:
        """Show ``port`` in the COM box: its list entry, with the description, when it is listed."""
        i = self.com.findData(port_name(port))
        if i >= 0:
            self.com.setCurrentIndex(i)
        else:
            self.com.setEditText(port)

    def show_config(self, ws: Workspace) -> None:
        widgets = (self.transport, self.host, self.port, self.com, self.baud, self.poll)
        for w in widgets:
            w.blockSignals(True)
        c = ws.config.connection
        self.transport.setCurrentText(c.transport)
        if c.transport == "serial":
            self.set_port(c.address)
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
            port = port_name(self.com.currentText())
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


PANELS = {"watch": "&Watch", "scope": "&Scope", "memory": "&Memory", "console": "&Console"}
PANEL_DOCK = {"scope": "Chart", "memory": "Memory", "console": "Console"}   # Watch is the central watch tabs


class ModeBar(QToolBar):
    """The panel buttons at the left of the toolbar: Watch, Scope, Memory, Console. Each one shows
    or hides its own panel (GUI-14)."""

    def __init__(self, main):
        super().__init__("Panels")
        self.setObjectName("modes")
        self.setMovable(False)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 6, 0)
        row.setSpacing(0)
        self.buttons = {}
        for k, (name, text) in enumerate(PANELS.items(), start=1):
            b = QToolButton()
            b.setText(text.replace("&", ""))
            b.setCheckable(True)
            b.setAutoRaise(False)
            b.setToolTip(f"Show or hide the {b.text()} panel (Ctrl+{k})")
            b.setObjectName("first" if k == 1 else "last" if k == len(PANELS) else "mid")
            b.clicked.connect(lambda _=False, n=name: main.toggle_panel(n))
            row.addWidget(b)
            self.buttons[name] = b
        self.addWidget(holder)
        holder.setStyleSheet(
            "QToolButton { padding: 3px 12px; border: 1px solid palette(mid); background: palette(base); }"
            "QToolButton:checked { background: palette(highlight); color: palette(highlighted-text);"
            " font-weight: bold; }"
            "QToolButton#first { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }"
            "QToolButton#last { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }")

    def show_panel(self, name: str, shown: bool) -> None:
        self.buttons[name].setChecked(shown)


class ErrorBanner(QToolBar):
    """One slim red bar under the toolbar for every error: an MCU whose ELF does not match its
    firmware (SYM-8; stays until it matches again) and the latest failed action (stays until
    closed, replaced, or a later action succeeds). It can't be moved or hidden by the user."""

    def __init__(self, main):
        super().__init__("Errors")
        self.setObjectName("error_banner")
        self.setMovable(False)
        self.setFloatable(False)
        self.toggleViewAction().setVisible(False)       # not in the toolbar context menu
        self.mismatch = ""
        self.error = ""
        box = QWidget()
        box.setObjectName("banner")
        box.setStyleSheet("#banner { background: #b3261e; border-radius: 3px; }"
                          "QLabel { color: white; background: transparent; }"
                          "QPushButton { color: #b3261e; background: white; border: none; border-radius: 3px;"
                          " padding: 1px 8px; }"
                          "QPushButton#close { color: white; background: transparent; font-weight: bold; }")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(8, 2, 4, 2)
        lay.setSpacing(6)
        self.text = QLabel()
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # a long error never widens the window
        lay.addWidget(self.text, 1)
        self.load_button = QPushButton("Load ELF...")
        self.load_button.setToolTip("Choose the ELF file that matches the firmware on the device")
        self.load_button.clicked.connect(main.edit_mcus)
        self.check_button = QPushButton("Check again")
        self.check_button.setToolTip("Compare the ELF with the device again, such as after flashing")
        self.check_button.clicked.connect(lambda: main.check_identity())
        self.close_button = QPushButton("\u2715")
        self.close_button.setObjectName("close")
        self.close_button.setToolTip("Dismiss this error")
        self.close_button.clicked.connect(lambda: self.show_error(""))
        for b in (self.load_button, self.check_button, self.close_button):
            lay.addWidget(b)
        self.addWidget(box)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._update()

    def show_mismatch(self, results) -> None:
        """Show the MCUs whose ELF does not match, or drop that line when none."""
        bad = [r for r in results if r.status == "mismatch"]
        self.mismatch = ("\u26a0 ELF does not match the firmware on " + ", ".join(r.mcu for r in bad) +
                         "; reads and writes to it are refused") if bad else ""
        self.setToolTip("\n".join(r.detail for r in bad))
        self._update()

    def show_error(self, msg: str) -> None:
        """Show the latest error, or clear it with an empty message."""
        self.error = msg
        self._update()

    def _update(self) -> None:
        lines = [x for x in (self.mismatch, self.error) if x]
        self.text.setText("\n".join(lines))
        self.load_button.setVisible(bool(self.mismatch))
        self.check_button.setVisible(bool(self.mismatch))
        self.close_button.setVisible(bool(self.error))
        self.setVisible(bool(lines))


# The connect button: text, border and background for each state.
STATE_COLORS = {
    "up": ("#3ccf7a", "#1f7a46", "#0d1f15"),
    "busy": ("#e0a030", "#8a6418", "#211a0c"),
    "down": ("#e05050", "#8a2a2a", "#210f0f"),
    "off": ("#7fb0ff", "#3a7bd5", "#101a2b"),       # disconnected
    "closed": ("#9a9a9a", "#3c3c3c", "#141414"),
}


def plug_icon(color: str) -> QIcon:
    """A small power-plug icon in ``color`` for the Connect and Disconnect button."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(11, 3, 11, 10)                  # prongs
    p.drawLine(21, 3, 21, 10)
    p.drawLine(6, 10, 26, 10)                  # top of the body
    p.drawArc(6, 2, 20, 18, 180 * 16, 180 * 16)  # rounded bottom of the body
    p.drawLine(16, 20, 16, 29)                 # cord
    p.end()
    return QIcon(pm)


LINK_TIPS = {   # the connect button's tooltip for each link state (LNK-9)
    LinkState.UP: "The port is open",
    LinkState.DOWN: "The port broke, such as a USB COM port unplugged; RDT reconnects by itself",
    LinkState.NO_REPLY: "The port is open but the device answered none of the last reads; "
                        "writes are refused until it answers again",
    LinkState.CLOSED: "Not connected",
}

IDENTITY_WORDS = {"match": "ELF matches the firmware", "mismatch": "ELF does NOT match the firmware",
                  "unchecked": "ELF not checked (nothing to compare)",
                  "unread": "ELF identity could not be read"}


def identity_summary(results) -> str:
    """One status-bar line for the identity checks (SYM-8), so a missing setup is not silent."""
    if not results:
        return IDENTITY_WORDS["unchecked"]
    if len(results) == 1:
        return IDENTITY_WORDS[results[0].status]
    return "; ".join(f"{r.mcu}: {IDENTITY_WORDS[r.status]}" for r in results)


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
        self._shown_identities: set[int] = set()
        self.log: CsvLog | None = None
        self._log_link_up = True
        self.ui = load_ui()

        self.chart = ChartPanel(self)
        self.chart.changed.connect(self._chart_changed)
        self.chart.series_changed.connect(self._chart_series_changed)
        self.watch_tabs = WatchTabs(self)
        self.watch_tabs.changed.connect(self.set_dirty)
        self.watch_tabs.sampled.connect(self.chart.feed)
        self.watch_tabs.sampled.connect(self._log_readings)
        self.watch_tabs.chart_requested.connect(self.add_to_chart)
        self.chart_pending: set[str] = set()   # dropped on the chart, charted once read (GUI-16)
        self.watch_tabs.sampled.connect(lambda _: self._chart_dropped())
        for w in (self.chart.plot.viewport(), self.chart.list.viewport()):
            DropTarget(w, self.drop_on_chart)
        self.setCentralWidget(self.watch_tabs)
        self.symbols = McuCards(self)
        self.symbols.add_requested.connect(lambda expr: self.watch_tabs.add_watch(expr))
        self.console = Console(self)
        self.memory = MemoryView(self)
        self.modes = ModeBar(self)
        self.addToolBar(self.modes)
        self.bar = ConnectionBar(self)
        self.addToolBar(self.bar)
        self.addToolBarBreak()
        self.banner = ErrorBanner(self)
        self.addToolBar(self.banner)
        self.docks = {}
        for title, widget, area in (("Target", self.symbols, Qt.LeftDockWidgetArea),
                                    ("Chart", self.chart, Qt.BottomDockWidgetArea),
                                    ("Console", self.console, Qt.BottomDockWidgetArea),
                                    ("Memory", self.memory, Qt.BottomDockWidgetArea)):
            dock = QDockWidget(title, self)
            dock.setObjectName(title.lower())
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            self.docks[title] = dock
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)   # the MCU cards run full height
        self.tabifyDockWidget(self.docks["Chart"], self.docks["Console"])
        self.tabifyDockWidget(self.docks["Console"], self.docks["Memory"])
        self.docks["Chart"].raise_()
        self.resizeDocks([self.docks["Chart"]], [300], Qt.Vertical)
        self.resizeDocks([self.docks["Target"]], [300], Qt.Horizontal)
        for dock in self.docks.values():   # a tab brought to the front or a panel shown or hidden by hand
            dock.visibilityChanged.connect(lambda _=False: QTimer.singleShot(0, self.sync_panel_buttons))
        self.tabifiedDockWidgetActivated.connect(lambda _=None: QTimer.singleShot(0, self.sync_panel_buttons))
        self.menu_bar = titlebar.install(self)
        self._menus()
        self.set_theme()
        self.restore_layout()
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

    def menuBar(self):
        """The menu bar inside the title bar row; QMainWindow.menuBar() would replace the row."""
        return self.menu_bar

    def _menus(self) -> None:
        def action(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            menu.addAction(a)
            return a

        m = self.menu_bar.addMenu("&File")
        action(m, "&New workspace", self.new_file, QKeySequence.New)
        action(m, "&Open...", self.open_file, QKeySequence.Open)
        action(m, "&Save", self.save, QKeySequence.Save)
        action(m, "Save &as...", self.save_as, QKeySequence.SaveAs)
        m.addSeparator()
        sub = m.addMenu("&Import")
        action(sub, "&Watch list...", lambda: self.import_watches())
        action(sub, "&Commands...", lambda: self.console.commands.import_file())
        action(sub, "&Interface settings...", lambda: self.import_ui())
        sub = m.addMenu("&Export")
        action(sub, "&Watch list...", lambda: self.export_watches())
        action(sub, "&Commands...", lambda: self.console.commands.export_file())
        action(sub, "&Interface settings...", lambda: self.export_ui())
        m.addSeparator()
        action(m, "&Quit", self.close, QKeySequence.Quit)
        m = self.menu_bar.addMenu("&Target")
        action(m, "&Connect or disconnect", self.toggle_connection, "F5")
        action(m, "SCPI &profile...", self.edit_profile)
        action(m, "&MCUs and ELF files...", self.edit_mcus)
        m.addSeparator()
        action(m, "Check ELF &identity", lambda: self.check_identity())
        m = self.menu_bar.addMenu("&Watch")
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
        m = self.menu_bar.addMenu("&View")
        for k, (name, text) in enumerate(PANELS.items(), start=1):
            action(m, f"Show or hide {text}", lambda _=False, n=name: self.toggle_panel(n), f"Ctrl+{k}")
        m.addAction(self.docks["Target"].toggleViewAction())
        action(m, "&Save layout", self.save_layout, "Ctrl+Shift+S")
        action(m, "Reset layout", self.reset_layout)
        m.addSeparator()
        stats = action(m, "&Statistics columns", self.watch_tabs.show_stats, "Ctrl+T")
        stats.setCheckable(True)
        sub = m.addMenu("&Theme")
        group = QActionGroup(self)
        self.theme_actions = {}
        for name, text in THEMES.items():
            a = action(sub, text, lambda _=False, n=name: self.set_theme(n))
            a.setCheckable(True)
            group.addAction(a)
            self.theme_actions[name] = a
        m = self.menu_bar.addMenu("&Help")
        action(m, "&Keyboard shortcuts", self.show_shortcuts, "F1")

    def shortcut_rows(self) -> list[tuple[str, str, str]]:
        """(where, what, keys) for every menu shortcut, read from the menus themselves, then the
        console's keys."""
        rows = []
        for top in self.menu_bar.actions():
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
        """Errors go to the red bar under the toolbar; other messages to the status bar, and
        they clear an earlier error since the later action went through."""
        self.banner.show_error(msg if error else "")
        self.message.setText("" if error else msg)

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
        path, _ = QFileDialog.getOpenFileName(self, "Open workspace", str(workspaces_folder()), FILTER)
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

    # --- panels (GUI-14) -----------------------------------------------------------------------

    def toggle_panel(self, name: str, on: bool | None = None) -> None:
        """Show or hide one panel: the watch tabs, the chart (Scope), the memory view or the
        console. A panel behind another tab is brought to the front instead of hidden."""
        if name == "watch":
            on = self.centralWidget().isHidden() if on is None else on
            self.centralWidget().setVisible(on)
            self.modes.show_panel("watch", on)
        else:
            dock = self.docks[PANEL_DOCK[name]]
            on = (not self._in_front(dock)) if on is None else on
            dock.setVisible(on)
            if on:
                dock.raise_()
        self.save_layout()

    def save_layout(self) -> None:
        """Remember which panels are shown and where they sit and how big they are."""
        self.ui.set_value("session/layout", bytes(self.saveState().toBase64()).decode())
        self.ui.set_value("session/watch_shown", not self.centralWidget().isHidden())

    def restore_layout(self) -> None:
        saved = self.ui.value("session/layout", "")
        if saved:
            self.restoreState(QByteArray.fromBase64(saved.encode()))
        self.centralWidget().setVisible(bool(self.ui.value("session/watch_shown", True)))
        self.sync_panel_buttons()

    def sync_panel_buttons(self) -> None:
        self.modes.show_panel("watch", not self.centralWidget().isHidden())
        for name, title in PANEL_DOCK.items():
            self.modes.show_panel(name, self._in_front(self.docks[title]))

    def _in_front(self, dock) -> bool:
        """Whether a panel is on screen: shown, and not behind another tab."""
        if dock.isHidden():
            return False
        return dock.visibleRegion().isEmpty() is False if self.isVisible() else True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.sync_panel_buttons)

    def reset_layout(self) -> None:
        """Show every panel again, where they start."""
        for key in ("session/layout", "session/watch_shown"):
            self.ui.values.pop(key, None)
        self.ui.save()
        d = self.docks
        for dock in d.values():   # take every panel out first, or Qt leaves stale tab bars behind
            dock.setFloating(False)
            self.removeDockWidget(dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, d["Target"])
        for t in ("Chart", "Console", "Memory"):
            self.addDockWidget(Qt.BottomDockWidgetArea, d[t])
        self.tabifyDockWidget(d["Chart"], d["Console"])
        self.tabifyDockWidget(d["Console"], d["Memory"])
        for dock in d.values():
            dock.setVisible(True)
        self.centralWidget().setVisible(True)
        d["Chart"].raise_()
        self.resizeDocks([d["Target"], d["Chart"]], [300, 300], Qt.Horizontal)
        self.resizeDocks([d["Chart"]], [300], Qt.Vertical)
        self.sync_panel_buttons()

    def set_theme(self, name: str | None = None) -> None:
        """Use a theme and remember it in ui.toml; with no name, the one ui.toml has (GUI-15)."""
        if name is None:
            name = str(self.ui.value("ui/theme", "system"))
        elif name != self.ui.value("ui/theme", "system"):
            self.ui.set_value("ui/theme", name)
        name = apply_theme(name)
        self.theme_actions[name].setChecked(True)
        self.chart.set_dark(is_dark())
        self.symbols.restyle()

    def settings(self) -> UiSettings:
        """Per-user interface settings (ui.toml), next to the console history."""
        return self.ui

    def remember(self, path: Path | None) -> None:
        """Remember the workspace file, to reopen it on the next start (WSP-3)."""
        if path is not None:
            self.ui.set_value("session/last_workspace", str(Path(path).resolve()))

    def reopen_last(self) -> bool:
        """Open the workspace that was open last time, if its file is still there."""
        last = self.ui.value("session/last_workspace", "")
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
        name = self.ws.path.name if self.ws.path else "bench.rdtw"
        path, _ = QFileDialog.getSaveFileName(self, "Save workspace", str(workspaces_folder() / name),
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

    # --- import and export -------------------------------------------------------------------

    def import_watches(self, path: str | Path | None = None, replace: bool | None = None) -> bool:
        """Take the watch tabs (and chart) of a watch list file or another workspace."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import watch list", "", WATCH_FILTER)
            if not path:
                return False
        if replace is None:
            replace = False
            if self.ws.watches:
                box = QMessageBox(QMessageBox.Question, "Import watch list",
                                  f"The workspace watches {len(self.ws.watches)} items in {len(self.ws.tabs)} tabs. "
                                  f"Add the file's tabs to them, or replace them (and the chart)?", parent=self)
                add = box.addButton("Add", QMessageBox.AcceptRole)
                rep = box.addButton("Replace", QMessageBox.DestructiveRole)
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if box.clickedButton() not in (add, rep):
                    return False
                replace = box.clickedButton() is rep
        try:
            added, skipped = self.ws.import_watches(path, replace)
        except ConfigError as e:
            QMessageBox.warning(self, "Import watch list", str(e))
            return False
        self.watch_tabs.rebuild()
        self.chart.load(self.ws.chart)
        self.set_dirty()
        self.status(f"Imported {added} watched items from {Path(path).name}"
                    + (f", skipped {skipped} already in their tab" if skipped else ""))
        return True

    def export_watches(self, path: str | Path | None = None) -> bool:
        if path is None:
            stem = self.ws.path.stem if self.ws.path else "watch"
            path, _ = QFileDialog.getSaveFileName(self, "Export watch list", f"{stem}.watch.toml",
                                                  "Watch list (*.watch.toml *.toml)")
            if not path:
                return False
        try:
            self.ws.export_watches(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Export watch list", str(e))
            return False
        self.status(f"Exported {len(self.ws.tabs)} tabs ({len(self.ws.watches)} items) to {Path(path).name}")
        return True

    def import_ui(self, path: str | Path | None = None) -> bool:
        """Take the interface settings in an exported file, and show them at once."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import interface settings", "", UI_FILTER)
            if not path:
                return False
        try:
            self.ui.import_file(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Import interface settings", str(e))
            return False
        self.console.apply_settings()
        self.memory.apply_settings()
        self.set_theme()
        self.status(f"Imported the interface settings from {Path(path).name}")
        return True

    def export_ui(self, path: str | Path | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export interface settings", "ui.toml", UI_FILTER)
            if not path:
                return False
        try:
            self.ui.export(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Export interface settings", str(e))
            return False
        self.status(f"Exported the interface settings to {Path(path).name}")
        return True

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        self.save_layout()
        for t in (self.poll_timer, self.state_timer, self.elf_timer, self.memory.timer, self.chart.timer):
            t.stop()
        self.stop_log()
        self.console.stop_log()
        self.device.shutdown()
        event.accept()

    # --- connection --------------------------------------------------------------------------

    def connection_edited(self, *_) -> None:
        self.set_dirty()
        self._show_state()

    def _link_name(self) -> str:
        """The link shown on the connect button: the COM port, or IP:port for TCP and UDP."""
        if self.device.target is not None:
            return self.device.target.config.connection.address
        try:
            return self.bar.connection().address
        except ConfigError:
            return ""

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
        self._show_state()

    def reconnect(self) -> None:
        if self.device.target is not None or self._connecting:
            self.connect()

    def _opened(self, t) -> None:
        self._connecting = False
        results = [m.identity for m in t.mcus.values() if m.identity is not None]
        self.status(f"Connected to {self.ws.config.connection.describe()}; " + identity_summary(results))
        self.show_identity(results)
        self.read_all()
        self._show_state()

    # --- ELF identity (SYM-8) ------------------------------------------------------------------

    def show_identity(self, results) -> None:
        """Note each MCU's identity check in the console; a mismatch also shows in the error bar
        (see _show_mismatch). There is no way around a mismatch in the GUI: load the matching ELF."""
        self._shown_identities = {id(r) for r in results}
        for r in results:
            self.console.log(str(r), "error" if r.status == "mismatch" else "note")
        self.banner.show_mismatch(results)

    def check_identity(self, mcus: list[str] | None = None) -> None:
        """Compare the ELFs with the firmware again, such as after flashing. Also runs by itself
        when the link or an MCU comes back, since the firmware may have been flashed meanwhile."""
        if self.device.target is None:
            self.status("Connect first to check the ELF identity", error=True)
            return

        def done(results):
            self.show_identity(results)
            if not any(r.status == "mismatch" for r in results):
                self.status(identity_summary(results))

        self.device.submit(lambda t: [r for name in (mcus or list(t.mcus)) for r in t.check_identity(name)],
                           done, lambda e: self.status(describe_error(e), True), key="identity")

    def _open_failed(self, e: Exception) -> None:
        self._connecting = False
        self.status(f"Cannot connect: {describe_error(e)}", error=True)
        self._show_state()

    def _show_state(self) -> None:
        state = self.device.state
        if self._connecting:
            word, look, click = "Connecting", "busy", "Click to cancel"
        elif state == LinkState.UP:
            word, look, click = "Connected", "up", "Click to disconnect"
        elif state in (LinkState.DOWN, LinkState.NO_REPLY):
            word, look, click = "Link down", "down", "Click to disconnect"
        else:
            word, look, click = "Disconnected", "off", "Click to connect"
        where = self._link_name()
        text = f"{word} \u00b7 {where}" if where else word
        if text != self._last_state:
            if self._last_state is not None:
                why = f" ({self.device.target.session.down_reason})" \
                    if look == "down" and self.device.target is not None else ""
                self.console.log(text + why, "error" if look == "down" else "note")
            self._last_state = text
        fg, border, bg = STATE_COLORS[look]
        tip = LINK_TIPS.get(state, "") if not self._connecting else ""
        button = self.bar.connect_button
        button.setText(text)
        button.setToolTip(f"{tip}\n{click} (F5)" if tip else f"{click} (F5)")
        button.setIcon(plug_icon(fg))
        button.setStyleSheet(f"QPushButton {{ color: {fg}; background: {bg}; border: 1px solid {border}; "
                             "border-radius: 4px; padding: 3px 12px }")
        up = state == LinkState.UP
        t = self.device.target
        if up and t is not None and not self._connecting and not self.device.busy("identity"):
            stale = t.stale_identities()
            if stale:
                self.check_identity(stale)
        self._show_mismatch(up)
        self.symbols.show_health(t, state if t is not None else None)
        self.memory.setEnabled(up)
        # With no reply the port is still open, so the console can send, such as to see whether
        # the device answers anything.
        self.console.set_link_up(up or state == LinkState.NO_REPLY)
        for w in (self.bar.transport, self.bar.host, self.bar.port, self.bar.com, self.bar.baud):
            w.setEnabled(self.device.target is None and not self._connecting)

    def _show_mismatch(self, up: bool) -> None:
        """An ELF that does not match its firmware shows in the error bar (SYM-8); each MCU's
        result shows on its card (GUI-14)."""
        t = self.device.target
        if not up or t is None:
            self.banner.show_mismatch([])
            return
        results = [m.identity for m in t.mcus.values() if m.identity is not None]
        self.banner.show_mismatch(results)
        if any(r.status == "mismatch" and id(r) not in self._shown_identities for r in results):
            self.show_identity(results)   # found by a check run before a poll, not by this window

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
        dlg = ProfileDialog(self.ws.config.profile, self.device, self, self.ws.config.profile_path)
        if dlg.exec() != QDialog.Accepted:
            return
        self.apply_profile(dlg.result_profile(), dlg.result_path())

    def apply_profile(self, profile: Profile, path: Path | None = None) -> None:
        self.ws.config.profile = profile
        self.ws.config.profile_path = path   # None: the profile is saved inside the workspace
        self.bar.show_profile(self.ws)
        self.set_dirty()
        self.reconnect()

    def edit_mcus(self) -> None:
        dlg = McuDialog(self.ws.config.mcus, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.ws.config.mcus = dlg.result_mcus()
        for tree in self.watch_tabs.trees:
            tree.retag()
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

    def show_memory(self, mcu: str) -> None:
        """The memory view, on one MCU."""
        self.memory.mcu.setCurrentText(mcu)
        self.docks["Memory"].show()
        self.docks["Memory"].raise_()

    def drop_on_chart(self, exprs: list[str]) -> None:
        """Expressions dragged onto the chart (GUI-16). One not watched yet goes to the tab in
        front first, since the chart plots what the watch tabs read; each is charted after its
        first read, when RDT knows whether it is a number."""
        watched = set(self.watch_tabs.exprs())
        for e in exprs:
            if any(tree.chartable(e) for tree in self.watch_tabs.trees):   # a value already shown
                self.chart_pending.add(e)
                continue
            if e not in watched and self.watch_tree.add_watch(e) is None:
                continue
            self.chart_pending.add(e)
            for tree in self.watch_tabs.trees:
                tree.read_now(e)
        self._chart_dropped()

    def _chart_dropped(self) -> None:
        for e in list(self.chart_pending):
            for tree in self.watch_tabs.trees:
                if tree.chartable(e):
                    self.chart_pending.discard(e)
                    tree.chart(e)
                    break
                st = tree.states.get(e)
                if st is not None and st.shown.get(e) is not None:   # read, but not a number
                    self.chart_pending.discard(e)
                    self.status(f"{e} is not a number, so it can't be charted")
                    break

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
    if sys.platform == "win32":           # own taskbar identity, not python.exe's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RDT.RemoteDebuggerTool")
        except Exception:
            pass
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "rdt.png")))
    win = MainWindow()
    if argv:
        win.open_path(argv[0])
    elif not win.reopen_last() and (local := default_target()) is not None:
        win.open_path(local)              # the CLI's default target file, as a starting point
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
