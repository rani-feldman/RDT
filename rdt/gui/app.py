"""RDT main window: connection bar, variable view, memory view, SCPI console and command buttons,
all working on one workspace (``.rdtw``).

    rdt-gui [bench.rdtw]
    python -m rdt.gui [bench.rdtw]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDockWidget, QFileDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QToolBar,
)

from ..errors import ConfigError
from ..profile import Profile
from ..session import LinkState
from ..target import ConnectionConfig, McuConfig, TargetConfig
from ..workspace import Workspace
from .device import Device, describe_error
from .panels import ButtonPanel, Console, McuDialog, MemoryView, ProfileDialog
from .variables import SymbolBrowser, WatchTree

FILTER = "RDT workspace (*.rdtw);;Target file (*.toml);;All files (*)"
TICK_MS = 50            # how often the watch tree looks for items that are due
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
        self.addWidget(self.transport)
        self._lan = [self.addWidget(self.host), self.addWidget(QLabel(":")), self.addWidget(self.port)]
        self._serial = [self.addWidget(self.com), self.addWidget(self.baud)]
        self.addSeparator()
        self.addWidget(self.profile)
        self.addWidget(self.connect_button)
        self.addWidget(self.state)
        self.addSeparator()
        self.addWidget(QLabel(" Poll "))
        self.addWidget(self.poll)
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

        self.watch_tree = WatchTree(self)
        self.watch_tree.changed.connect(self.set_dirty)
        self.setCentralWidget(self.watch_tree)
        self.symbols = SymbolBrowser(self)
        self.symbols.add_requested.connect(self.watch_tree.add_watch)
        self.console = Console(self)
        self.memory = MemoryView(self)
        self.buttons = ButtonPanel(self)
        self.buttons.changed.connect(self.set_dirty)
        self.bar = ConnectionBar(self)
        self.addToolBar(self.bar)
        self.docks = {}
        for title, widget, area in (("Variables", self.symbols, Qt.LeftDockWidgetArea),
                                    ("Console", self.console, Qt.BottomDockWidgetArea),
                                    ("Memory", self.memory, Qt.BottomDockWidgetArea),
                                    ("Buttons", self.buttons, Qt.RightDockWidgetArea)):
            dock = QDockWidget(title, self)
            dock.setObjectName(title.lower())
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            self.docks[title] = dock
        self.tabifyDockWidget(self.docks["Console"], self.docks["Memory"])
        self.docks["Console"].raise_()
        self.resizeDocks([self.docks["Console"]], [220], Qt.Vertical)
        self.resizeDocks([self.docks["Variables"], self.docks["Buttons"]], [250, 150], Qt.Horizontal)
        self._menus()
        self.message = QLabel()
        self.statusBar().addWidget(self.message, 1)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.watch_tree.tick)
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
        m = self.menuBar().addMenu("&Watch")
        action(m, "&Add expression...", self.add_expression, "Ctrl+E")
        action(m, "&Read all now", self.read_all, "F6")
        action(m, "Re&move selected", lambda: [self.watch_tree.remove_watch(e) for e in self.watch_tree.selected_exprs()],
               QKeySequence.Delete)
        m = self.menuBar().addMenu("&View")
        for dock in self.docks.values():
            m.addAction(dock.toggleViewAction())

    # --- workspace ---------------------------------------------------------------------------

    def show_workspace(self) -> None:
        self.bar.show_config(self.ws)
        self.watch_tree.rebuild()
        self.symbols.set_workspace(self.ws)
        self.memory.set_workspace(self.ws)
        self.buttons.rebuild()
        self.set_dirty(False)

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

    def load(self, ws: Workspace) -> None:
        if self.device.target is not None:
            self.device.close()
        self.ws = ws
        self.show_workspace()
        self.status(f"Opened {ws.path}" if ws.path else "New workspace")

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
        self.status(f"Saved {path}")
        return True

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        for t in (self.poll_timer, self.state_timer, self.elf_timer, self.memory.timer):
            t.stop()
        self.device.shutdown()
        event.accept()

    # --- connection --------------------------------------------------------------------------

    def connection_edited(self, *_) -> None:
        self.set_dirty()

    def _take_connection(self) -> None:
        self.ws.config.connection = self.bar.connection()

    def set_poll_ms(self, ms: int) -> None:
        if ms != self.ws.poll_ms:
            self.ws.poll_ms = ms
            self.set_dirty()
            for st in self.watch_tree.states.values():
                self.watch_tree._decorate(st)
            if self.memory.auto.isChecked():
                self.memory._auto(True)

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
        self.status(f"Connected to {t.idn or self.ws.config.connection.describe()}")
        self.read_all()
        self._show_state()

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
        self.bar.state.setText(text)
        self.bar.state.setStyleSheet(f"color: {color}")
        self.bar.connect_button.setText(button)
        up = state == LinkState.UP
        for w in (self.memory, self.console, self.buttons):
            w.setEnabled(up)
        for w in (self.bar.transport, self.bar.host, self.bar.port, self.bar.com, self.bar.baud):
            w.setEnabled(self.device.target is None and not self._connecting)

    def _check_elfs(self) -> None:
        if self.device.target is None or self.device.busy("elfs"):
            return

        def reloaded(mcus):
            if mcus:
                self.status(f"ELF rebuilt for {', '.join(mcus)}; watch items found again by name")
                self.watch_tree.remap(mcus)
                self.symbols.reload()

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

    def read_all(self) -> None:
        for expr in list(self.watch_tree.states):
            self.watch_tree.read_now(expr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.WARNING)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("RDT")
    app.setOrganizationName("RDT")
    win = MainWindow()
    if argv:
        win.open_path(argv[0])
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
