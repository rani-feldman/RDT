"""The smaller panels: memory view, SCPI console, and the dialogs for the SCPI
profile and the MCU list."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..errors import CommandTimeout, ConfigError, LinkDown, RdtError
from ..profile import DATA_FORMATS, Profile, parse_int
from ..target import McuConfig, Target
from ..workspace import Workspace
from .device import describe_error

BYTES_PER_ROW = 16


def mono() -> QFont:
    return QFontDatabase.systemFont(QFontDatabase.FixedFont)


def run_command(t: Target, text: str) -> str | None:
    """Send one SCPI command typed by hand. Returns the reply; for a query that got none, a note
    with what the error queue says (the firmware answers a failed command with silence)."""
    reply = t.command(text)
    if reply is not None:
        return reply
    if not text.split(None, 1)[0].endswith("?"):
        return None
    try:
        err = t.error_query()
    except (CommandTimeout, LinkDown):
        err = None
    return "(no reply" + (f"; error queue: {err}" if err else "") + ")"


# --- SCPI console ----------------------------------------------------------------------------


class _History(QObject):
    """Up and Down in the console's input line walk through the commands sent before."""

    def __init__(self, line: QLineEdit):
        super().__init__(line)
        self.line = line
        self.items: list[str] = []
        self.pos = 0
        line.installEventFilter(self)

    def add(self, text: str) -> None:
        if not self.items or self.items[-1] != text:
            self.items.append(text)
        self.pos = len(self.items)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Up, Qt.Key_Down):
            if not self.items:
                return True
            step = -1 if event.key() == Qt.Key_Up else 1
            self.pos = max(0, min(len(self.items), self.pos + step))
            self.line.setText(self.items[self.pos] if self.pos < len(self.items) else "")
            return True
        return False


class Console(QWidget):
    """Send any SCPI command by hand and see the reply."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.output = QPlainTextEdit(readOnly=True)
        self.output.setFont(mono())
        self.output.setMaximumBlockCount(5000)
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setFont(mono())
        self.input.setPlaceholderText("SCPI command, such as *IDN?  (Up and Down for history)")
        self.input.returnPressed.connect(self._send_typed)
        self.history = _History(self.input)
        send = QPushButton("Send")
        send.clicked.connect(self._send_typed)
        errors = QPushButton("Error queue")
        errors.setToolTip("Read SYST:ERR? until the queue is empty")
        errors.clicked.connect(self.drain_errors)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.output.clear)
        row.addWidget(self.input, 1)
        for b in (send, errors, clear):
            row.addWidget(b)
        layout.addLayout(row)

    def log(self, text: str) -> None:
        self.output.appendPlainText(text)
        self.output.moveCursor(QTextCursor.End)

    def _send_typed(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.history.add(text)
        self.input.clear()
        self.send(text)

    def send(self, text: str) -> None:
        self.log(f"> {text}")
        self.main.device.submit(lambda t: run_command(t, text),
                                lambda reply: self.log(reply if reply is not None else "(sent)"),
                                lambda e: self.log(f"error: {describe_error(e)}"))

    def drain_errors(self) -> None:
        def job(t: Target):
            out = []
            for _ in range(64):
                err = t.error_query()
                if err is None:
                    break
                out.append(str(err))
            return out or ['0,"No error"']

        self.log("> error queue")
        self.main.device.submit(job, lambda lines: self.log("\n".join(lines)),
                                lambda e: self.log(f"error: {describe_error(e)}"))


# --- memory view -----------------------------------------------------------------------------


class MemoryView(QWidget):
    """Hex and ASCII dump of raw memory. Typing a new byte in a hex cell writes it."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.mcu = QComboBox()
        self.address = QLineEdit("0x20000000")
        self.address.setToolTip("An address, or a variable name of the chosen MCU")
        self.address.returnPressed.connect(self.read)
        self.length = QSpinBox()
        self.length.setRange(1, 65536)
        self.length.setValue(256)
        self.length.setSuffix(" bytes")
        read = QPushButton("Read")
        read.clicked.connect(self.read)
        self.auto = QCheckBox("Refresh")
        self.auto.setToolTip("Read again at the global polling interval")
        self.auto.toggled.connect(self._auto)
        for w, stretch in ((QLabel("MCU"), 0), (self.mcu, 0), (QLabel("Address"), 0), (self.address, 1),
                           (self.length, 0), (read, 0), (self.auto, 0)):
            row.addWidget(w, stretch)
        layout.addLayout(row)
        self.table = QTableWidget(0, BYTES_PER_ROW + 1)
        self.table.setFont(mono())
        self.table.setHorizontalHeaderLabels([f"{i:X}" for i in range(BYTES_PER_ROW)] + ["ASCII"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setFont(mono())
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.itemChanged.connect(self._edited)
        layout.addWidget(self.table, 1)
        self.message = QLabel("")
        layout.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.read)
        self.base: int | None = None
        self.data = b""
        self._shown_mcu = ""

    def set_workspace(self, ws: Workspace) -> None:
        self.mcu.clear()
        for m in ws.config.mcus:
            self.mcu.addItem(m.name)

    def _auto(self, on: bool) -> None:
        if on and self.main.ws.poll_ms:
            self.timer.start(self.main.ws.poll_ms)
            self.read()
        else:
            self.timer.stop()

    def read(self) -> None:
        mcu, text, length = self.mcu.currentText(), self.address.text().strip(), self.length.value()
        if not mcu or not text:
            return

        def job(t: Target):
            try:
                addr = parse_int(text)
            except ValueError:
                addr = t.resolve(f"{mcu}:{text}").addr
            return mcu, addr, t.read(mcu, addr, length)

        self.main.device.submit(job, self._show, self._failed, key="memory")

    def _failed(self, e: Exception) -> None:
        self.message.setText(describe_error(e))

    def _show(self, result) -> None:
        mcu, base, data = result
        self._shown_mcu, self.base, self.data = mcu, base, data
        start = base - base % BYTES_PER_ROW
        rows = (base + len(data) - start + BYTES_PER_ROW - 1) // BYTES_PER_ROW
        self.table.blockSignals(True)
        self.table.setRowCount(rows)
        self.table.setVerticalHeaderLabels([f"{start + r * BYTES_PER_ROW:08X}" for r in range(rows)])
        for r in range(rows):
            text = []
            for c in range(BYTES_PER_ROW):
                addr = start + r * BYTES_PER_ROW + c
                i = addr - base
                item = self.table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(r, c, item)
                if 0 <= i < len(data):
                    item.setText(f"{data[i]:02X}")
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                    text.append(chr(data[i]) if 32 <= data[i] < 127 else ".")
                else:
                    item.setText("")
                    item.setFlags(Qt.NoItemFlags)
                    text.append(" ")
            ascii_item = QTableWidgetItem("".join(text))
            ascii_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, BYTES_PER_ROW, ascii_item)
        self.table.blockSignals(False)
        self.message.setText(f"{mcu}: {len(data)} bytes at 0x{base:08X}")

    def _edited(self, item: QTableWidgetItem) -> None:
        if self.base is None or item.column() >= BYTES_PER_ROW:
            return
        start = self.base - self.base % BYTES_PER_ROW
        addr = start + item.row() * BYTES_PER_ROW + item.column()
        try:
            value = int(item.text().strip(), 16)
            if not 0 <= value <= 0xFF:
                raise ValueError
        except ValueError:
            self.message.setText(f"{item.text()!r} is not a byte; type two hex digits such as 7F")
            self.read()
            return
        mcu = self._shown_mcu

        def done(_):
            self.main.status(f"{mcu}:0x{addr:08X} = 0x{value:02X}")
            self.read()

        def failed(e):
            self.main.status(f"write {mcu}:0x{addr:08X}: {describe_error(e)}", error=True)
            self.read()

        self.main.device.submit(lambda t: t.write(mcu, addr, bytes([value])), done, failed)


# --- SCPI profile editor ---------------------------------------------------------------------

_OPTIONAL = {"size_query", "reply_regex", "error_query", "probe"}


def _escape(s: str) -> str:
    return s.encode("unicode_escape").decode("ascii")


def _unescape(s: str) -> str:
    return s.encode("ascii").decode("unicode_escape")


class ProfileDialog(QDialog):
    """Edit the SCPI profile: the command templates and the reply format. The preview shows the
    commands as they will be sent; Test sends a read with the edited profile."""

    def __init__(self, profile: Profile, device, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SCPI profile")
        self.device = device
        self.ranges = profile.ranges
        self.edits: dict[str, QWidget] = {}
        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)
        tips = {
            "read": "Placeholders: {mcu} {addr} {len}",
            "write": "Placeholders: {mcu} {addr} {value} {size}",
            "value_format": "How {value} is written, such as 0x{value:X} or {value}",
            "addr_format": "How {addr} is written, such as 0x{addr:08X}",
            "reply_regex": "Optional; the group named data (or group 1) holds the data",
            "terminator": "Escapes allowed: \\n, \\r\\n",
        }
        for f in fields(Profile):
            if f.name == "ranges":
                continue
            value = getattr(profile, f.name)
            if f.name == "data_format" or f.name == "byte_order":
                w = QComboBox()
                w.addItems(DATA_FORMATS if f.name == "data_format" else ("little", "big"))
                w.setCurrentText(value)
                w.currentTextChanged.connect(self._preview)
            elif f.name == "max_write":
                w = QComboBox()
                w.addItems(["1", "2", "4", "8"])
                w.setCurrentText(str(value))
                w.currentTextChanged.connect(self._preview)
            else:
                text = _escape(value) if f.name == "terminator" else ("" if value is None else
                                                                     f"{value:g}" if isinstance(value, float) else str(value))
                w = QLineEdit(text)
                if f.name in _OPTIONAL:
                    w.setPlaceholderText("none")
                if f.name in ("read", "write", "value_format", "addr_format", "reply_regex"):
                    w.setFont(mono())
                w.textChanged.connect(self._preview)
            if f.name in tips:
                w.setToolTip(tips[f.name])
            self.edits[f.name] = w
            form.addRow(f.name.replace("_", " ").capitalize(), w)
        self.preview = QLabel()
        self.preview.setFont(mono())
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setWordWrap(True)
        outer.addWidget(self.preview)
        test_row = QHBoxLayout()
        self.test_mcu = QSpinBox()
        self.test_mcu.setRange(0, 255)
        self.test_addr = QLineEdit("0x20000000")
        self.test_len = QSpinBox()
        self.test_len.setRange(1, 4096)
        self.test_len.setValue(16)
        self.test_button = QPushButton("Test read")
        self.test_button.clicked.connect(self.test)
        for w in (QLabel("MCU"), self.test_mcu, QLabel("Address"), self.test_addr, self.test_len, self.test_button):
            test_row.addWidget(w)
        outer.addLayout(test_row)
        self.test_result = QLabel("")
        self.test_result.setFont(mono())
        self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.test_result)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._preview()

    def result_profile(self) -> Profile:
        """The profile as edited; raises ConfigError or ValueError when a field is wrong."""
        values = {}
        for name, w in self.edits.items():
            text = w.currentText() if isinstance(w, QComboBox) else w.text()
            if name == "terminator":
                values[name] = _unescape(text)
            elif name in ("max_write", "max_response", "retries"):
                values[name] = int(text)
            elif name == "timeout_ms":
                values[name] = float(text)
            elif name in _OPTIONAL:
                values[name] = text.strip() or None
            else:
                values[name] = text
        return Profile(**values, ranges=self.ranges)

    def _preview(self) -> None:
        try:
            p = self.result_profile()
        except (ConfigError, ValueError, UnicodeError) as e:
            self.preview.setText(f"⚠ {e}")
            self.preview.setStyleSheet("color: #c03030")
            self.ok.setEnabled(False)
            self.test_button.setEnabled(False)
            return
        self.preview.setStyleSheet("")
        self.ok.setEnabled(True)
        self.test_button.setEnabled(self.device.target is not None)
        self.preview.setText(f"Read 16 bytes:  {p.format_read(0, 0x20000000, 16)}\n"
                             f"Write 2 bytes:  {p.format_write(0, 0x20000000, 0x1234, 2)}")

    def test(self) -> None:
        try:
            p = self.result_profile()
            mcu, addr, length = self.test_mcu.value(), parse_int(self.test_addr.text()), self.test_len.value()
        except (ConfigError, ValueError) as e:
            self.test_result.setText(str(e))
            return
        cmd = p.format_read(mcu, addr, length)

        def job(t: Target):
            reply = t.command(cmd, expect_reply=True, timeout_ms=p.timeout_ms)
            if reply is None:
                return f"> {cmd}\n(no reply)"
            try:
                data = p.decode_data(reply.encode("latin-1"), length)
            except RdtError as e:
                return f"> {cmd}\n< {reply[:200]}\n⚠ {e}"
            return f"> {cmd}\n< {reply[:200]}\nOK: {data.hex(' ').upper()}"

        self.test_result.setText(f"> {cmd}")
        self.device.submit(job, self.test_result.setText, lambda e: self.test_result.setText(describe_error(e)))


# --- MCU list --------------------------------------------------------------------------------


class McuDialog(QDialog):
    """The target's MCUs: id, name, ELF file and timeout. MCU 0 is the main MCU."""

    COLS = ("Id", "Name", "ELF file", "Timeout ms")

    def __init__(self, mcus: list[McuConfig], parent=None):
        super().__init__(parent)
        self.setWindowTitle("MCUs and ELF files")
        self.resize(640, 260)
        self.originals = {m.id: m for m in mcus}
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table)
        for m in mcus:
            self._add_row(m.id, m.name, str(m.elf or ""), "" if m.timeout_ms is None else f"{m.timeout_ms:g}")
        row = QHBoxLayout()
        for text, slot in (("Add MCU", self._add), ("Remove", self._remove), ("Browse ELF...", self._browse)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, mcu_id: int, name: str, elf: str, timeout: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, text in enumerate((str(mcu_id), name, elf, timeout)):
            self.table.setItem(r, c, QTableWidgetItem(text))

    def _add(self) -> None:
        ids = {int(self.table.item(r, 0).text()) for r in range(self.table.rowCount())
               if self.table.item(r, 0).text().isdigit()}
        new = next(i for i in range(256) if i not in ids)
        self._add_row(new, "main" if new == 0 else f"mcu{new}", "", "")

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedItems()}, reverse=True):
            self.table.removeRow(r)

    def _browse(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        path, _ = QFileDialog.getOpenFileName(self, "ELF file", "", "ELF files (*.elf *.axf *.out);;All files (*)")
        if path:
            self.table.item(r, 2).setText(path)

    def _accept(self) -> None:
        try:
            self.result_mcus()
        except (ConfigError, ValueError) as e:
            QMessageBox.warning(self, "MCUs", str(e))
            return
        self.accept()

    def result_mcus(self) -> list[McuConfig]:
        out = []
        for r in range(self.table.rowCount()):
            text = [self.table.item(r, c).text().strip() if self.table.item(r, c) else "" for c in range(4)]
            try:
                mcu_id = int(text[0])
            except ValueError:
                raise ConfigError(f"row {r + 1}: the id must be a number") from None
            if not text[1]:
                raise ConfigError(f"MCU {mcu_id} needs a name")
            elf = Path(text[2]) if text[2] else None
            timeout = float(text[3]) if text[3] else None
            old = self.originals.get(mcu_id)
            base = old if old is not None else McuConfig(mcu_id, text[1])
            out.append(replace(base, id=mcu_id, name=text[1], elf=elf, timeout_ms=timeout))
        ids = [m.id for m in out]
        if 0 not in ids:
            raise ConfigError("MCU 0, the main MCU, is required")
        if len(set(ids)) != len(ids) or len({m.name for m in out}) != len(out):
            raise ConfigError("MCU ids and names must be unique")
        return sorted(out, key=lambda m: m.id)
