"""The smaller panels: memory view, SCPI console, and the dialogs for the SCPI
profile and the MCU list."""

from __future__ import annotations

from collections import deque
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QSplitter, QStyledItemDelegate, QTableWidgetItem, QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..console import CommandHistory, SessionLog, completions, hex_to_text, text_to_hex
from ..errors import ConfigError, RdtError, WriteRefused
from ..memfmt import INT_TYPES, MemFormat
from ..profile import Profile, align_span, parse_int
from ..target import MCU_COLORS, McuConfig, Target, check_color, check_short, mcu_short
from ..workspace import Workspace
from .device import describe_error
from .style import fixed_font
from .terminal import Terminal

BYTES_PER_ROW = 16
TRANSCRIPT_LINES = 5000
MAX_MATCHES = 1000
FONT_MIN, FONT_MAX = 6, 32   # console font size, in points
# Console keys for Help > Keyboard shortcuts (CON-12); keep in step with the bindings in Console.
CONSOLE_KEYS = [
    ("Enter", "Send the command"),
    ("Up / Down", "Walk the command history"),
    ("\u2192", "Take the grey suggestion shown while typing"),
    ("Tab / Ctrl+Space", "Complete the command; with several matches, list them all"),
    ("Tab / Enter in the list", "Take the highlighted command (Tab with none highlighted takes the first)"),
    ("Ctrl+F", "Find in the transcript"),
    ("Enter / Shift+Enter", "Next / previous match, in the find bar"),
    ("Esc", "Close the find bar"),
    ("Ctrl+K", "Clear the transcript"),
    ("Ctrl+= / Ctrl+- / Ctrl+wheel", "Larger / smaller console text"),
    ("Ctrl+0", "Default console text size"),
]
# The console is a terminal (CON-13) in the theme's colours: dark in the dark themes, light in the
# light ones (rani, 2026-10-17). Console.restyle switches the three tables below.
TERMINAL_DARK = ({"background": "#0d1117", "text": "#c9d1d9", "stamp": "#6e7681", "prompt": "#3fb950",
                  "border": "#30363d", "placeholder": "#6e7681", "selection": "#264f78"},
                 {"tx": "#7ee787", "rx": "#c9d1d9", "error": "#ff7b72", "note": "#8b949e"},
                 {"tx": ("TX", "#3fb950"), "rx": ("RX", "#58a6ff"), "error": ("ERR", "#f85149"),
                  "note": ("SYS", "#8b949e")})
TERMINAL_LIGHT = ({"background": "#ffffff", "text": "#1f2328", "stamp": "#6e7781", "prompt": "#1a7f37",
                   "border": "#d0d7de", "placeholder": "#8c959f", "selection": "#b6d7ff"},
                  {"tx": "#116329", "rx": "#1f2328", "error": "#cf222e", "note": "#6e7781"},
                  {"tx": ("TX", "#1a7f37"), "rx": ("RX", "#0969da"), "error": ("ERR", "#a40e26"),
                   "note": ("SYS", "#6e7781")})
TERMINAL = dict(TERMINAL_DARK[0])
LINE_COLORS = dict(TERMINAL_DARK[1])
LINE_TAGS = dict(TERMINAL_DARK[2])
TAG_WIDTH = 4


def _common_prefix(words: list[str]) -> str:
    """The longest start all the words share, ignoring case; taken from the first word."""
    first = words[0]
    n = len(first)
    for w in words[1:]:
        n = min(n, len(w))
        for i in range(n):
            if w[i].casefold() != first[i].casefold():
                n = i
                break
    return first[:n]


def mono() -> QFont:
    return fixed_font()


def run_command(t: Target, text: str) -> str | None:
    """Send one SCPI command typed by hand. Returns the reply, or a note for a query that got none
    (the firmware answers a failed command with silence)."""
    reply = t.command(text)
    if reply is not None or not text.split(None, 1)[0].endswith("?"):
        return reply
    return "(no reply)"


# --- SCPI console ----------------------------------------------------------------------------


class HistoryPanel(QWidget):
    """The saved console history, newest first, beside the transcript like the commands list:
    send an entry again, delete it, or clear them all."""

    def __init__(self, console: "Console"):
        super().__init__()
        self.console = console
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("History, newest first"))
        self.list = QListWidget()
        self.list.setFont(mono())
        self.list.itemDoubleClicked.connect(lambda _item: self.send())
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        # Lambdas, so the button's checked flag is not passed on.
        for text, slot in (("Send", lambda: self.send()), ("Delete", lambda: self.delete()),
                           ("Clear all", lambda: self.clear_all())):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)

    def set_open(self, on: bool) -> None:
        if on:
            self.refresh()
        self.setVisible(on)

    def refresh(self) -> None:
        self.list.clear()
        self.list.addItems(list(reversed(self.console.history.items)))
        self.list.setCurrentRow(0)

    def _current(self) -> str | None:
        item = self.list.currentItem()
        return item.text() if item else None

    def send(self) -> None:
        text = self._current()
        if text:
            self.console.send_command(text)
            self.refresh()

    def delete(self) -> None:
        text = self._current()
        if text:
            row = self.list.currentRow()
            self.console.history.remove(text)
            self.refresh()
            self.list.setCurrentRow(min(row, self.list.count() - 1))

    def clear_all(self) -> None:
        if QMessageBox.question(self, "Console history", "Delete the whole console history?") == QMessageBox.Yes:
            self.console.history.clear()
            self.refresh()


class CommandsPanel(QWidget):
    """The workspace's known commands (CON-7), by group. Double-click puts one in the input line."""

    def __init__(self, console: "Console"):
        super().__init__()
        self.console = console
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit(placeholderText="Filter commands")
        self.filter.textChanged.connect(lambda _t: self.refresh())
        layout.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Command", "Name"])
        self.tree.setFont(mono())
        self.tree.setRootIsDecorated(True)
        self.tree.itemActivated.connect(self._activated)
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        # Lambdas, so the button's checked flag is not passed on as the file path.
        for text, tip, slot in (("Import...", "Add commands from a commands file (.toml), a CSV file or a text file",
                                 lambda: self.import_file()),
                                ("Export...", "Write the list to a commands file (.toml) or a CSV file",
                                 lambda: self.export_file()),
                                ("Clear", "Empty the list", lambda: self.clear())):
            b = QPushButton(text, toolTip=tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)

    @property
    def ws(self) -> Workspace:
        return self.console.main.ws

    def refresh(self) -> None:
        self.tree.clear()
        needle = self.filter.text().casefold()
        groups: dict[str, QTreeWidgetItem] = {}
        for c in self.ws.commands:
            if needle and needle not in f"{c.command} {c.name} {c.group} {c.description}".casefold():
                continue
            parent = self.tree.invisibleRootItem()
            if c.group:
                if c.group not in groups:
                    groups[c.group] = QTreeWidgetItem(self.tree, [c.group])
                    groups[c.group].setFirstColumnSpanned(True)
                parent = groups[c.group]
            item = QTreeWidgetItem(parent, [c.command, c.name])
            item.setData(0, Qt.UserRole, c.command)
            if c.description:
                item.setToolTip(0, c.description)
                item.setToolTip(1, c.description)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def _activated(self, item: QTreeWidgetItem, _col: int) -> None:
        command = item.data(0, Qt.UserRole)
        if command:
            self.console.pick(command)

    def import_file(self, path: str | Path | None = None, replace: bool | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import commands", "",
                                                  "Command lists (*.toml *.csv *.txt);;All files (*)")
            if not path:
                return False
        if replace is None:
            replace = False
            if self.ws.commands:
                box = QMessageBox(QMessageBox.Question, "Import commands",
                                  f"The list has {len(self.ws.commands)} commands. Add the new ones to it, "
                                  f"or replace it?", parent=self)
                add = box.addButton("Add", QMessageBox.AcceptRole)
                rep = box.addButton("Replace", QMessageBox.DestructiveRole)
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if box.clickedButton() not in (add, rep):
                    return False
                replace = box.clickedButton() is rep
        try:
            added, skipped = self.ws.import_commands(path, replace)
        except ConfigError as e:
            QMessageBox.warning(self, "Import commands", f"Cannot import {path}: {e}")
            return False
        self.refresh()
        self.console.main.set_dirty()
        self.console.note(f"imported {added} commands from {Path(path).name}"
                          + (f", skipped {skipped} already listed" if skipped else ""))
        return True

    def export_file(self, path: str | Path | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export commands", "commands.toml",
                                                  "Commands file (*.toml);;CSV files (*.csv)")
            if not path:
                return False
        try:
            self.ws.export_commands(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Export commands", str(e))
            return False
        self.console.note(f"exported {len(self.ws.commands)} commands to {Path(path).name}")
        return True

    def clear(self) -> None:
        if self.ws.commands and QMessageBox.question(
                self, "Commands", f"Remove all {len(self.ws.commands)} commands from the list?") == QMessageBox.Yes:
            self.ws.commands = []
            self.refresh()
            self.console.main.set_dirty()


class Console(QWidget):
    """Send any SCPI command by hand and see the reply."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        self.ui = main.ui   # per-user view settings (CON-11), in ui.toml next to the console history
        self.commands = CommandsPanel(self)
        self.commands.hide()
        self.history_panel = HistoryPanel(self)
        self.history_panel.hide()
        self.entries: deque[tuple[datetime, str, str]] = deque(maxlen=TRANSCRIPT_LINES)
        self.history = CommandHistory()
        self.session_log: SessionLog | None = None
        layout.addLayout(self._tool_row())

        # The terminal: transcript, then the prompt as its last line (CON-13).
        self.terminal = QWidget(objectName="terminal")
        self.terminal.setAttribute(Qt.WA_StyledBackground, True)
        term = QVBoxLayout(self.terminal)
        term.setContentsMargins(4, 2, 4, 2)
        term.setSpacing(0)
        # One text area: the transcript, and its last line is the prompt you type on (CON-14).
        self.output = Terminal()
        self.output.setFont(mono())
        self.output.setMaximumBlockCount(TRANSCRIPT_LINES)
        self._terminal_colors()
        self.output.viewport().installEventFilter(self)
        self.output.setContextMenuPolicy(Qt.CustomContextMenu)
        self.output.customContextMenuRequested.connect(self._context_menu)
        self.input = self.output          # the prompt line's text(), setText() and signals
        self.output.returnPressed.connect(self._send_typed)
        self.output.textEdited.connect(self._typed)
        self.output.history_step.connect(self._history_step)
        self.output.complete_requested.connect(self.complete)
        term.addWidget(self.output, 1)
        self.suggest_model = QStringListModel(self)
        self.completer = QCompleter(self.suggest_model, self)
        self.completer.setWidget(self.output)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.activated.connect(self._apply_completion)
        self.output.popup_open = lambda: self.completer.popup().isVisible()
        self.output.popup_key = self._popup_key

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.terminal)
        split.addWidget(self.commands)
        split.addWidget(self.history_panel)        # with both open, side by side
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 1)
        layout.addWidget(split, 1)
        self._find_bar(layout)
        self.set_link_up(False)
        for keys, slot in (("Ctrl+F", self.show_find), ("Ctrl+K", self.clear), ("Ctrl+=", lambda: self.zoom(1)),
                           ("Ctrl++", lambda: self.zoom(1)), ("Ctrl+-", lambda: self.zoom(-1)),
                           ("Ctrl+0", lambda: self.set_font_size(mono().pointSize()))):
            QShortcut(QKeySequence(keys), self, slot, context=Qt.WidgetWithChildrenShortcut)
        self.apply_settings()

    def apply_settings(self) -> None:
        """Show the text size and wrapping kept in the interface settings."""
        self.set_font_size(int(self.ui.value("console/font_size", mono().pointSize())))
        self.wrap.setChecked(bool(self.ui.value("console/wrap", False)))
        self.set_wrap(self.wrap.isChecked())

    def _tool_row(self) -> QHBoxLayout:
        """The console's buttons, above the terminal. Sending needs none: press Enter."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.commands_button = QPushButton("Commands")
        self.commands_button.setCheckable(True)
        self.commands_button.setToolTip("The workspace's list of known commands: import, pick, export")
        self.commands_button.toggled.connect(self.commands.setVisible)
        self.history_button = history = QPushButton("History")
        history.setCheckable(True)
        history.setToolTip("The saved console history, beside the transcript: send an entry again or delete it")
        history.toggled.connect(self.history_panel.set_open)
        clear = QPushButton("Clear")
        clear.setToolTip("Clear the transcript (Ctrl+K)")
        clear.clicked.connect(lambda: self.clear())
        self.timestamps = QCheckBox("Timestamps")
        self.timestamps.setToolTip("Show the time of every line, to the millisecond")
        self.timestamps.setChecked(True)
        self.timestamps.toggled.connect(lambda _on: self.rerender())
        self.wrap = QCheckBox("Wrap")
        self.wrap.setToolTip("Wrap long lines; off, they scroll sideways")
        self.wrap.setChecked(bool(self.ui.value("console/wrap", False)))
        self.wrap.toggled.connect(self.set_wrap)
        self.log_button = QPushButton("Log to file")
        self.log_button.setCheckable(True)
        self.log_button.setToolTip("Append every console line, with date and time, to a text file")
        self.log_button.clicked.connect(self._toggle_log)
        for w in (self.commands_button, history, clear, self.log_button):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.timestamps)
        row.addWidget(self.wrap)
        return row

    # --- font size and wrap ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if (event.type() == QEvent.Wheel and obj is self.output.viewport()
                and event.modifiers() & Qt.ControlModifier):
            self.zoom(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(obj, event)

    def font_size(self) -> int:
        return self.output.font().pointSize()

    def zoom(self, step: int) -> None:
        self.set_font_size(self.font_size() + step)

    def set_font_size(self, size: int) -> None:
        size = max(FONT_MIN, min(FONT_MAX, size))
        for w in (self.output,):
            f = w.font()
            f.setPointSize(size)
            w.setFont(f)
        self.ui.set_value("console/font_size", size)

    def set_wrap(self, on: bool) -> None:
        self.output.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)
        self.ui.set_value("console/wrap", on)

    # --- hex and text ------------------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        menu = self.output.createStandardContextMenu(pos)
        selected = self.output.textCursor().selectedText().replace("\u2029", "\n")
        menu.addSeparator()
        for text, convert in (("Selection as hex", text_to_hex), ("Hex selection as text", hex_to_text)):
            act = menu.addAction(text, lambda c=convert, t=text: self.show_converted(t, c, selected))
            act.setEnabled(bool(selected))
        menu.addSeparator()
        act = menu.addAction("Send break (250 ms)", self.send_break)
        act.setEnabled(self.output.input_enabled and self.main.ws.config.connection.transport == "serial")
        act.setToolTip("Hold the COM port line in the break state; only on a COM port link")
        menu.exec(self.output.mapToGlobal(pos))

    def show_converted(self, title: str, convert, selected: str) -> QDialog | None:
        try:
            result = convert(selected)
        except ValueError as e:
            QMessageBox.warning(self, title, str(e))
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(480, 200)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit(result, readOnly=True)
        view.setFont(mono())
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = buttons.addButton("Copy", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(result))
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        return dlg

    # --- search ------------------------------------------------------------------------------

    def _find_bar(self, layout: QVBoxLayout) -> None:
        self.find_bar = QWidget()
        row = QHBoxLayout(self.find_bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.find_text = QLineEdit()
        self.find_text.setPlaceholderText("Find in transcript  (Enter next, Shift+Enter previous, Esc close)")
        self.find_text.textChanged.connect(lambda _t: self._search(jump=True))
        self.find_text.returnPressed.connect(self.find_next)
        QShortcut(QKeySequence("Shift+Return"), self.find_text, self.find_previous, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("Escape"), self.find_bar, self.hide_find, context=Qt.WidgetWithChildrenShortcut)
        self.find_count = QLabel("")
        row.addWidget(self.find_text, 1)
        row.addWidget(self.find_count)
        for text, tip, slot in (("▲", "Previous match (Shift+Enter)", self.find_previous),
                                ("▼", "Next match (Enter)", self.find_next),
                                ("✕", "Close (Esc)", self.hide_find)):
            b = QToolButton(text=text, toolTip=tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        self.find_bar.hide()
        layout.addWidget(self.find_bar)
        self.matches: list[QTextCursor] = []
        self.match = -1

    def show_find(self) -> None:
        self.find_bar.show()
        selected = self.output.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.find_text.setText(selected)
        self.find_text.setFocus()
        self.find_text.selectAll()
        self._search()

    def hide_find(self) -> None:
        self.find_bar.hide()
        self.matches, self.match = [], -1
        self.output.setExtraSelections([])
        self.output._to_end()
        self.input.setFocus()

    def find_next(self) -> None:
        self._step(1)

    def find_previous(self) -> None:
        self._step(-1)

    def _search(self, jump: bool = False) -> None:
        """Find every match (case-insensitive), highlight them all and the current one more strongly."""
        text = self.find_text.text()
        doc = self.output.document()
        self.matches = []
        if text and not self.find_bar.isHidden():
            cursor = doc.find(text, 0)
            while not cursor.isNull() and len(self.matches) < MAX_MATCHES:
                self.matches.append(cursor)
                cursor = doc.find(text, cursor)
        if not self.matches:
            self.match = -1
        elif jump or self.match < 0:
            self.match = len(self.matches) - 1     # the newest match, at the bottom
        else:
            self.match = min(self.match, len(self.matches) - 1)
        self._highlight()

    def _step(self, step: int) -> None:
        if not self.matches:
            self._search()
        if self.matches:
            self.match = (self.match + step) % len(self.matches)
        self._highlight()

    def _highlight(self) -> None:
        sels = []
        for i, cursor in enumerate(self.matches):
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format.setBackground(QColor("#ff9632" if i == self.match else "#ffe46b"))
            sel.format.setForeground(QColor("#000000"))
            sels.append(sel)
        self.output.setExtraSelections(sels)
        if self.match >= 0:
            self.output.setTextCursor(QTextCursor(self.matches[self.match]))
            self.output.ensureCursorVisible()
            self.find_count.setText(f"{self.match + 1} of {len(self.matches)}"
                                    + ("+" if len(self.matches) >= MAX_MATCHES else ""))
        else:
            self.find_count.setText("no match" if self.find_text.text() else "")

    # --- autocomplete ------------------------------------------------------------------------

    def set_link_up(self, up: bool) -> None:
        """Sending needs the link; the transcript, search, log and the command list don't."""
        self.link_up = up
        self.output.input_enabled = up
        self.output.hint = "type a command \u2014 \u2191 recalls history, \u2192 takes the suggestion, Tab lists matches" if up \
            else "not connected"
        self._show_prompt()

    def _show_prompt(self) -> None:
        """The prompt names the link, such as COM3 or 192.168.1.20:5025: green while it is up."""
        ws = getattr(self.main, "ws", None)
        color = TERMINAL["prompt"] if getattr(self, "link_up", False) else TERMINAL["stamp"]
        self.output.set_prompt(ws.config.connection.address if ws else "", color)

    def set_workspace(self, ws: Workspace) -> None:
        self.commands.refresh()
        self._show_prompt()

    def suggestions(self) -> list[str]:
        """Completions for the input line: the history, newest first, then the known commands."""
        ws = getattr(self.main, "ws", None)
        saved = [c.command for c in ws.commands] if ws else []
        return completions(self.input.text(), self.history.items, None, saved, common=False)

    def pick(self, command: str) -> None:
        """A command picked from the list goes to the input line, to be completed and sent."""
        self.input.setText(command)
        self.input.setFocus()

    def complete(self) -> None:
        """Tab: one suggestion is filled in at once. With several, the part they all start with is
        filled in and a list opens to pick from (Tab or Enter takes the highlighted one)."""
        found = self.suggestions()
        if len(found) == 1:
            self._apply_completion(found[0])
            return
        if not found:
            return
        typed = self.input.text()
        starts = [c for c in found if c.casefold().startswith(typed.strip().casefold())]
        common = _common_prefix(starts) if starts else ""
        if len(common) > len(typed):
            self.input.setText(common)
        self.suggest_model.setStringList(found)
        self.output.suggestion = ""
        self.completer.complete(self._popup_rect())

    def _popup_rect(self):
        """Where the list opens: under the cursor, wide enough for its longest command (the
        completer takes the popup's width from this rectangle, and the cursor is 1 pixel wide)."""
        rect = self.output.cursorRect()
        popup = self.completer.popup()
        width = popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width() + 2 * popup.frameWidth() + 8
        rect.setWidth(max(width, 200))
        return rect

    def _typed(self, _text: str) -> None:
        """As you type, the best suggestion shows in grey after the cursor (CON-14): the newest
        history entry, else the first known command, that starts with what is typed."""
        self.history.reset_navigation()
        found = self.suggestions() if self.input.text().strip() else []
        typed = self.input.text().casefold()
        self.output.suggestion = next((c for c in found if c.casefold().startswith(typed)), "")
        self.output.viewport().update()
        # The list of matches opens as you type, without Tab; a command typed out in full that
        # matches only itself needs no list.
        if not found or (len(found) == 1 and found[0].casefold() == typed.strip()):
            self.completer.popup().hide()
            return
        self.suggest_model.setStringList(found)
        self.completer.complete(self._popup_rect())

    def _popup_key(self, key) -> bool:
        """Tab, Enter or Esc while the list of matches shows. Tab takes the highlighted command,
        or the first when none is highlighted; Enter takes the highlighted one, and with none
        highlighted sends what is typed; Esc closes the list. True when the key was used here."""
        popup = self.completer.popup()
        current = popup.currentIndex()
        if key == Qt.Key_Escape:
            popup.hide()
            return True
        if current.isValid():
            self._apply_completion(current.data())
            return True
        if key == Qt.Key_Tab and self.suggest_model.rowCount():
            self._apply_completion(self.suggest_model.index(0).data())
            return True
        popup.hide()
        return False

    def _history_step(self, step: int) -> None:
        if self.completer.popup().isVisible():
            return
        self.output.suggestion = ""
        self.input.setText(self.history.navigate(step, self.input.text()))

    def _apply_completion(self, text: str) -> None:
        self.completer.popup().hide()
        self.output.suggestion = ""
        self.input.setText(text)
        self.output.setFocus()

    # --- transcript --------------------------------------------------------------------------

    def log(self, text: str, kind: str = "rx") -> None:
        """Add a line: ``tx`` a command sent, ``rx`` a reply, ``error`` or ``note`` (link events)."""
        entry = (datetime.now(), kind, text)
        self.entries.append(entry)
        if self.session_log is not None:
            self.session_log.write(kind, text, entry[0])
        self._render(entry)
        if not self.find_bar.isHidden():
            self._search()

    def start_log(self, path: str | Path) -> bool:
        self.stop_log()
        try:
            self.session_log = SessionLog(path)
        except OSError as e:
            QMessageBox.warning(self, "Console log", f"Cannot write {path}: {e}")
            self.log_button.setChecked(False)
            return False
        self.log_button.setChecked(True)
        self.log_button.setToolTip(f"Logging to {path}. Click to stop.")
        self.note(f"logging to {path}")
        return True

    def stop_log(self) -> None:
        if self.session_log is not None:
            path = self.session_log.path
            self.session_log.close()
            self.session_log = None
            self.note(f"log saved to {path}")
        self.log_button.setChecked(False)
        self.log_button.setToolTip("Append every console line, with date and time, to a text file")

    def _toggle_log(self, on: bool) -> None:
        if not on:
            self.stop_log()
            return
        default = datetime.now().strftime("console_%Y%m%d_%H%M%S.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Log console to", default, "Text files (*.txt);;All files (*)",
                                              options=QFileDialog.DontConfirmOverwrite)
        if not path:
            self.log_button.setChecked(False)
            return
        self.start_log(path)

    def note(self, text: str) -> None:
        self.log(text, "note")

    def clear(self) -> None:
        self.entries.clear()
        self.output.clear_transcript()
        self._search()

    def restyle(self, dark: bool) -> None:
        """The terminal in the theme's colours: dark or light. The transcript is drawn again."""
        terminal, lines, tags = TERMINAL_DARK if dark else TERMINAL_LIGHT
        if terminal == TERMINAL:
            return
        TERMINAL.update(terminal)
        LINE_COLORS.update(lines)
        LINE_TAGS.update(tags)
        self._terminal_colors()
        self._show_prompt()
        self.rerender()

    def _terminal_colors(self) -> None:
        self.terminal.setStyleSheet(
            f"#terminal {{ background: {TERMINAL['background']}; border: 1px solid {TERMINAL['border']}; }}"
            f"#terminal QPlainTextEdit, #terminal QLineEdit {{ background: {TERMINAL['background']};"
            f" color: {TERMINAL['text']}; border: none; selection-background-color: {TERMINAL['selection']}; }}"
            f"#terminal QLabel {{ color: {TERMINAL['stamp']}; }}")
        self.output.text_color = TERMINAL["text"]
        self.output.ghost_color = TERMINAL["placeholder"]
        self.output.arrow_color = TERMINAL["stamp"]

    def rerender(self) -> None:
        self.output.clear_transcript()
        for entry in self.entries:
            self._render(entry)
        self._search()

    def _render(self, entry: tuple[datetime, str, str]) -> None:
        """One line per entry: time, a tag (TX, RX, ERR, SYS) and the text. The lines of a
        multi-line reply line up under the first one's text."""
        when, kind, text = entry
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(LINE_COLORS.get(kind) or TERMINAL["text"]))
        if kind == "note":
            fmt.setFontItalic(True)
        stamp_fmt = QTextCharFormat()
        stamp_fmt.setForeground(QColor(TERMINAL["stamp"]))
        tag, tag_color = LINE_TAGS.get(kind, LINE_TAGS["rx"])
        tag_fmt = QTextCharFormat()
        tag_fmt.setForeground(QColor(tag_color))
        stamp = when.strftime("%H:%M:%S.%f")[:-3] + " " if self.timestamps.isChecked() else ""
        head = stamp + tag.ljust(TAG_WIDTH)
        follow = self.output._cursor_in_input()
        cursor = self.output.insert_above()
        for i, line in enumerate(text.split("\n")):
            if i == 0:
                cursor.insertText(stamp, stamp_fmt)
                cursor.insertText(tag.ljust(TAG_WIDTH), tag_fmt)
            else:
                cursor.insertText(" " * len(head), stamp_fmt)
            cursor.insertText(line, fmt)
            cursor.insertBlock()
        self.output.done_inserting(follow)

    def _send_typed(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.completer.popup().hide()
        self.input.setText("")
        self.send_command(text)

    def send_command(self, text: str) -> None:
        """Send a command typed (or picked from the history) by hand and keep it in the history."""
        self.history.add(text)
        if not self.history_panel.isHidden():
            self.history_panel.refresh()
        self.send(text)

    def send(self, text: str) -> None:
        self.log(text, "tx")
        self.main.device.submit(lambda t: run_command(t, text), self._reply,
                                lambda e: self.log(f"error: {describe_error(e)}", "error"))

    def _reply(self, reply: str | None) -> None:
        if reply is None:
            self.log("(sent)", "note")
        else:
            self.log(reply, "error" if reply.startswith("(no reply") else "rx")

    def send_break(self) -> None:
        self.log("break (250 ms)", "tx")
        self.main.device.submit(lambda t: t.send_break(250), lambda _r: self.note("break sent"),
                                lambda e: self.log(f"error: {describe_error(e)}", "error"))


# --- memory view -----------------------------------------------------------------------------


class _CellDelegate(QStyledItemDelegate):
    """The memory cells' editor. Qt reloads an open editor whenever its cell changes, so with
    Refresh on a new read would replace what is being typed (M4): the editor is filled once, when
    it opens, and the view knows which cell is being edited so a read leaves that cell alone."""

    def __init__(self, view: "MemoryView"):
        super().__init__(view.table)
        self.view = view

    def createEditor(self, parent, opt, idx):
        self.view._editing = (idx.row(), idx.column())
        return super().createEditor(parent, opt, idx)

    def setEditorData(self, editor, idx) -> None:
        if not editor.property("rdt_filled"):
            super().setEditorData(editor, idx)
            editor.setProperty("rdt_filled", True)


class MemoryView(QWidget):
    """Dump of raw memory, 16 bytes per row with their ASCII. Right-click picks how the cells show the
    bytes (GUI-7): hex bytes by default, unsigned or signed C integers in hex or decimal, characters,
    floats or doubles. The ASCII column is shown only beside the hex bytes, the default view (GUI-8).
    Typing a new value in a cell writes it."""

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
        self.table = QTableWidget(0, 0)
        self.table.setFont(mono())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setFont(mono())
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.itemChanged.connect(self._edited)
        self._editing: tuple[int, int] | None = None   # (row, column) of the cell being edited
        cells = _CellDelegate(self)
        cells.closeEditor.connect(self._editor_closed)
        self.table.setItemDelegate(cells)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.table, 1)
        self.message = QLabel("")
        layout.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.read)
        self.base: int | None = None
        self.data = b""
        self.order = "little"
        self._shown_mcu = ""
        self.format = self._load_format()
        self._layout_columns()

    # --- display format ------------------------------------------------------------------------

    def _load_format(self) -> MemFormat:
        s = self.main.settings()
        try:
            return MemFormat(str(s.value("memory/kind", "unsigned")), str(s.value("memory/type", "Char")),
                             bool(s.value("memory/decimal", False)))
        except ValueError:
            return MemFormat()

    def apply_settings(self) -> None:
        """Show the display format kept in the interface settings."""
        self.set_format(self._load_format())

    def set_format(self, fmt: MemFormat) -> None:
        """Show the cells in another format, and remember it for the next start."""
        self.format = fmt
        s = self.main.settings()
        s.set_value("memory/kind", fmt.kind)
        s.set_value("memory/type", fmt.type)
        s.set_value("memory/decimal", fmt.decimal)
        self._layout_columns()
        if self.base is not None:
            self._show((self._shown_mcu, self.base, self.data, self.order))

    def _layout_columns(self) -> None:
        size = self.format.size
        self.table.setColumnCount(BYTES_PER_ROW // size + 1)
        self.table.setHorizontalHeaderLabels([f"{i * size:X}" for i in range(BYTES_PER_ROW // size)] + ["ASCII"])
        ascii_shown = self.format == MemFormat()      # only beside hex bytes
        self.table.setColumnHidden(BYTES_PER_ROW // size, not ascii_shown)
        # stretch the ASCII column only; with it hidden, the last value column keeps its own width
        self.table.horizontalHeader().setStretchLastSection(ascii_shown)

    def _menu(self, pos) -> None:
        fmt = self.format
        menu = QMenu(self)

        def add(parent: QMenu, text: str, target: MemFormat) -> None:
            a = parent.addAction(text)
            a.setCheckable(True)
            a.setChecked(target == fmt)     # a check mark, not a radio dot: the menu is rebuilt each time
            a.triggered.connect(lambda: self.set_format(target))

        decimal = menu.addAction("Decimal")
        decimal.setCheckable(True)
        decimal.setChecked(fmt.decimal)
        decimal.setEnabled(fmt.kind in ("unsigned", "signed"))
        decimal.setToolTip("Show the integer formats in decimal instead of hex")
        decimal.toggled.connect(lambda on: self.set_format(replace(self.format, decimal=on)))
        for kind in ("unsigned", "signed"):
            sub = menu.addMenu(kind.capitalize())
            for name in INT_TYPES:
                add(sub, name, MemFormat(kind, name, fmt.decimal))
        menu.addSeparator()
        for kind in ("ascii", "float", "double"):
            add(menu, kind.capitalize(), MemFormat(kind, fmt.type, fmt.decimal))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # --- reading -------------------------------------------------------------------------------

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
            return mcu, addr, t.read(mcu, addr, length), t.mcu(mcu).byte_order

        self.main.device.submit(job, self._show, self._failed, key="memory")

    def _failed(self, e: Exception) -> None:
        self.message.setText(describe_error(e))

    def _row_start(self) -> int:
        return self.base - self.base % BYTES_PER_ROW

    def _show(self, result) -> None:
        mcu, base, data, order = result
        self._shown_mcu, self.base, self.data, self.order = mcu, base, data, order
        fmt, size = self.format, self.format.size
        cells = BYTES_PER_ROW // size
        start = self._row_start()
        rows = (base + len(data) - start + BYTES_PER_ROW - 1) // BYTES_PER_ROW
        self.table.blockSignals(True)
        self.table.setRowCount(rows)
        self.table.setVerticalHeaderLabels([f"{start + r * BYTES_PER_ROW:08X}" for r in range(rows)])
        for r in range(rows):
            text = []
            for c in range(cells):
                i = start + r * BYTES_PER_ROW + c * size - base
                item = self.table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(r, c, item)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if (r, c) == self._editing:
                    text.append(fmt.to_ascii(data[i:i + size], order) if 0 <= i and i + size <= len(data)
                                else " " * size)
                    continue   # shown once the editor closes
                if 0 <= i and i + size <= len(data):     # a cell only partly read stays blank
                    item.setText(fmt.to_text(data[i:i + size], order))
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                    text.append(fmt.to_ascii(data[i:i + size], order))
                else:
                    item.setText("")
                    item.setFlags(Qt.NoItemFlags)
                    text.append(" " * size)
            ascii_item = QTableWidgetItem((" " if size > 1 else "").join(text))
            ascii_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, cells, ascii_item)
        self.table.blockSignals(False)
        self.message.setText(f"{mcu}: {len(data)} bytes at 0x{base:08X} as {fmt.label}")

    def _editor_closed(self, *_) -> None:
        """A cell's editor closed, committed or not: show what was read last in it again."""
        self._editing = None
        if self.base is not None:
            self._show((self._shown_mcu, self.base, self.data, self.order))

    # --- writing -------------------------------------------------------------------------------

    def _edited(self, item: QTableWidgetItem) -> None:
        fmt = self.format
        if self.base is None or item.column() >= BYTES_PER_ROW // fmt.size:
            return
        addr = self._row_start() + item.row() * BYTES_PER_ROW + item.column() * fmt.size
        try:
            data = fmt.from_text(item.text(), self.order)
        except ValueError as e:
            self.message.setText(str(e))
            self.read()
            return
        mcu = self._shown_mcu
        try:
            self.main.ws.check_raw_write(mcu, addr)
        except WriteRefused as e:
            self.main.status(str(e), error=True)
            self.read()
            return
        shown = fmt.to_text(data, self.order)
        ws = self.main.ws
        end = addr + len(data)
        # Memory-view writes are raw (D-WS-4): no scaling and no min/max, which are in an item's
        # shown units. But bytes of a watched item marked read-only are not written, and an item
        # that asks before writing is asked for here too (M12; rani, 2026-10-18, code review).
        guarded = [w for w in ws.watches if w.confirm or ws.read_only_reason(w.expr)]

        def overlapping(t: Target) -> list[str]:
            hits = []
            for w in guarded:
                try:
                    loc = t.resolve(w.expr)
                except RdtError:
                    continue   # not on this device now: it has no bytes to protect
                if loc.mcu == mcu and loc.addr < end and addr < loc.addr + max(loc.size, 1):
                    hits.append(w.expr)
            return hits

        def done(_):
            self.main.status(f"{mcu}:0x{addr:08X} = {shown} ({fmt.label})")
            self.read()

        def failed(e):
            self.main.status(f"write {mcu}:0x{addr:08X}: {describe_error(e)}", error=True)
            self.read()

        def checked(hits: list[str]) -> None:
            locked = [e for e in hits if ws.read_only_reason(e)]
            if locked:
                self.main.status(f"{mcu}:0x{addr:08X}: not written, it is part of {', '.join(locked)}, which is "
                                 f"{ws.read_only_reason(locked[0])}", error=True)
                self.read()
                return
            ask = [e for e in hits if (w := ws.watch(e)) is not None and w.confirm]
            if ask and QMessageBox.question(
                    self, "Write memory", f"Write {shown} to {mcu}:0x{addr:08X}? It is part of {', '.join(ask)}."
            ) != QMessageBox.Yes:
                self.main.status(f"{mcu}:0x{addr:08X}: write cancelled")
                self.read()
                return
            self.main.device.submit(lambda t: t.write(mcu, addr, data), done, failed)

        if not guarded:
            checked([])
            return
        self.main.device.submit(overlapping, checked, failed)


# --- SCPI profile editor ---------------------------------------------------------------------

def _escape(s: str) -> str:
    return s.encode("unicode_escape").decode("ascii")


def _unescape(s: str) -> str:
    return s.encode("ascii").decode("unicode_escape")


class ProfileDialog(QDialog):
    """Edit the SCPI profile: the command templates and the reply format. The preview shows the
    commands as they will be sent; Test sends a read with the edited profile. Load from file fills
    the fields from a profile file. The workspace keeps its profile in its own file beside the
    .rdtw (bench.profile.toml), written on save (rani, 2026-10-15)."""

    def __init__(self, profile: Profile, device, parent=None, path: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("SCPI profile")
        self.device = device
        self.ranges = profile.ranges
        self.path = path              # the workspace's profile file, if it has one yet
        self.edits: dict[str, QWidget] = {}
        outer = QVBoxLayout(self)
        file_row = QHBoxLayout()
        self.file_label = QLabel(f"File: {path.name}" if path else "File: saved with the workspace")
        self.file_label.setToolTip(str(path) if path else "")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.load_button = QPushButton("Load from file...")
        self.load_button.setToolTip("Fill the fields from a profile file (.toml with an [scpi] table)")
        self.load_button.clicked.connect(self.load_file)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.load_button)
        outer.addLayout(file_row)
        form = QFormLayout()
        outer.addLayout(form)
        tips = {
            "read": "Placeholders: {mcu} {addr} {len}. {addr} is sent as bare hex, such as 20000000",
            "write": "Placeholders: {mcu} {addr} {value} {size}. {addr} and {value} are sent as bare hex",
            "terminator": "Escapes allowed: \\n, \\r\\n",
            "align": "Every read and write covers whole aligned words of this many bytes; "
                     "a smaller value is read as its word and written by read-modify-write",
        }
        for f in fields(Profile):
            if f.name == "ranges":
                continue
            value = getattr(profile, f.name)
            if f.name == "byte_order":
                w = QComboBox()
                w.addItems(("little", "big"))
                w.setCurrentText(value)
                w.currentTextChanged.connect(self._preview)
            elif f.name in ("max_write", "align"):
                w = QComboBox()
                w.addItems(["1", "2", "4", "8"])
                w.setCurrentText(str(value))
                w.currentTextChanged.connect(self._preview)
            else:
                text = _escape(value) if f.name == "terminator" else ("" if value is None else
                                                                     f"{value:g}" if isinstance(value, float) else str(value))
                w = QLineEdit(text)
                if f.name in ("read", "write"):
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

    def set_profile(self, profile: Profile) -> None:
        for name, w in self.edits.items():
            value = getattr(profile, name)
            if isinstance(w, QComboBox):
                w.setCurrentText(str(value))
            elif name == "terminator":
                w.setText(_escape(value))
            else:
                w.setText("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))
        self.ranges = profile.ranges

    def load_file(self, path: str | None = None) -> None:
        if not path:
            start = str(self.path.parent) if self.path else ""
            path, _ = QFileDialog.getOpenFileName(self, "Load SCPI profile", start, "Profile (*.toml);;All files (*)")
            if not path:
                return
        try:
            profile = Profile.load(path)
        except ConfigError as e:
            QMessageBox.warning(self, "Load profile", str(e))
            return
        self.set_profile(profile)
        self.file_label.setText(f"Loaded from {Path(path).name}")
        self.file_label.setToolTip(str(path))
        self._preview()

    def result_profile(self) -> Profile:
        """The profile as edited; raises ConfigError or ValueError when a field is wrong."""
        values = {}
        for name, w in self.edits.items():
            text = w.currentText() if isinstance(w, QComboBox) else w.text()
            if name == "terminator":
                values[name] = _unescape(text)
            elif name in ("max_write", "align", "max_response", "retries"):
                values[name] = int(text)
            elif name == "timeout_ms":
                values[name] = float(text)
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
                             f"Write {p.align} bytes:  {p.format_write(0, 0x20000000, 0x1234, p.align)}")

    def test(self) -> None:
        try:
            p = self.result_profile()
            mcu, addr, length = self.test_mcu.value(), parse_int(self.test_addr.text()), self.test_len.value()
        except (ConfigError, ValueError) as e:
            self.test_result.setText(str(e))
            return
        start, end = align_span(addr, length, p.align)   # the firmware takes only whole aligned words
        cmd = p.format_read(mcu, start, end - start)
        asked, length = length, end - start

        def job(t: Target):
            # a test read is a read like any other: refused outside the ELF sections and the listed
            # ranges, and on an MCU whose ELF does not match its firmware (L9)
            m = next((m for m in t.mcus.values() if m.id == mcu), None)
            if m is None:
                return f"> {cmd}\n⚠ no MCU with id {mcu} in this target, so the read is refused"
            try:
                m.memory.check_aligned(addr, asked, p.align)   # with the edited profile's word size
            except RdtError as e:
                return f"> {cmd}\n⚠ MCU {m.name}: {e}"
            try:
                t._require_image(m)
            except RdtError as e:
                return f"> {cmd}\n⚠ {e}"
            reply = t.command(cmd, expect_reply=True, timeout_ms=p.timeout_ms)
            if reply is None:
                return f"> {cmd}\n(no reply)"
            try:
                order = m.byte_order
                data = p.decode_data(reply.encode("latin-1"), length, order)
            except RdtError as e:
                return f"> {cmd}\n< {reply[:200]}\n⚠ {e}"
            return f"> {cmd}\n< {reply[:200]}\nOK: {data.hex(' ').upper()}"

        self.test_result.setText(f"> {cmd}")
        self.device.submit(job, self.test_result.setText, lambda e: self.test_result.setText(describe_error(e)))


# --- MCU list --------------------------------------------------------------------------------


class McuDialog(QDialog):
    """The target's MCUs: id, name, short name and colour (GUI-12), ELF file and timeout. MCU 0 is
    the main MCU."""

    COLS = ("Id", "Name", "Short name", "Colour", "ELF file", "Timeout ms")
    COL_ID, COL_NAME, COL_SHORT, COL_COLOR, COL_ELF, COL_TIMEOUT = range(6)

    def __init__(self, mcus: list[McuConfig], parent=None):
        super().__init__(parent)
        self.setWindowTitle("MCUs and ELF files")
        self.resize(1200, 280)
        self.originals = {m.id: m for m in mcus}
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        # The ELF path takes the spare width on one line, cut in the middle so the file name stays
        # visible; the full path is the cell's tooltip (word wrap cut it to "C:..." before).
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.horizontalHeader().setSectionResizeMode(self.COL_ELF, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        layout.addWidget(self.table)
        for m in mcus:
            self._add_row(m.id, m.name, str(m.elf or ""), "" if m.timeout_ms is None else f"{m.timeout_ms:g}",
                          m.short or "", m.color or "")
        self.table.horizontalHeaderItem(self.COL_SHORT).setToolTip(
            "Shown on the MCU's tags, such as CM7. Empty: the part of the name after the last underscore")
        self.table.horizontalHeaderItem(self.COL_COLOR).setToolTip(
            "The MCU's tag colour. Double-click to choose; empty: a colour picked by its id")
        self.table.horizontalHeaderItem(self.COL_TIMEOUT).setToolTip(
            "How long to wait for this MCU's reply. Empty: the SCPI profile's timeout")
        self.table.cellDoubleClicked.connect(self._double_clicked)
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

    def _add_row(self, mcu_id: int, name: str, elf: str, timeout: str, short: str = "",
                 color: str = "") -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, text in ((self.COL_ID, str(mcu_id)), (self.COL_NAME, name), (self.COL_SHORT, short),
                        (self.COL_COLOR, color), (self.COL_ELF, elf), (self.COL_TIMEOUT, timeout)):
            self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.item(r, self.COL_ELF).setToolTip(elf)
        self._show_color(r)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == self.COL_ELF and item.toolTip() != item.text():
            item.setToolTip(item.text())

    def _show_color(self, r: int) -> None:
        """Paint the colour cell with the colour the MCU's tag will have."""
        item = self.table.item(r, self.COL_COLOR)
        text = item.text().strip()
        try:
            mcu_id = int(self.table.item(r, self.COL_ID).text())
        except (ValueError, AttributeError):
            mcu_id = r
        color = QColor(text if QColor.isValidColorName(text) else MCU_COLORS[mcu_id % len(MCU_COLORS)])
        item.setBackground(color)
        item.setForeground(QColor("white"))

    def _double_clicked(self, r: int, c: int) -> None:
        if c != self.COL_COLOR:
            return
        item = self.table.item(r, c)
        color = QColorDialog.getColor(item.background().color(), self, "Tag colour")
        if color.isValid():
            item.setText(color.name())
            self._show_color(r)

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
            self.table.item(r, self.COL_ELF).setText(path)

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
            text = [self.table.item(r, c).text().strip() if self.table.item(r, c) else ""
                    for c in range(len(self.COLS))]
            try:
                mcu_id = int(text[self.COL_ID])
            except ValueError:
                raise ConfigError(f"row {r + 1}: the id must be a number") from None
            if not text[self.COL_NAME]:
                raise ConfigError(f"MCU {mcu_id} needs a name")
            short = check_short(text[self.COL_SHORT]) if text[self.COL_SHORT] else None
            color = check_color(text[self.COL_COLOR]) if text[self.COL_COLOR] else None
            elf = Path(text[self.COL_ELF]) if text[self.COL_ELF] else None
            timeout = float(text[self.COL_TIMEOUT]) if text[self.COL_TIMEOUT] else None
            old = self.originals.get(mcu_id)
            base = old if old is not None else McuConfig(mcu_id, text[self.COL_NAME])
            out.append(replace(base, id=mcu_id, name=text[self.COL_NAME], elf=elf, timeout_ms=timeout,
                               short=short, color=color))
        ids = [m.id for m in out]
        if 0 not in ids:
            raise ConfigError("MCU 0, the main MCU, is required")
        if len(set(ids)) != len(ids) or len({m.name for m in out}) != len(out):
            raise ConfigError("MCU ids and names must be unique")
        shorts = [mcu_short(m).lower() for m in out]
        if len(set(shorts)) != len(shorts):
            raise ConfigError("MCU short names must be unique; type one for each MCU that shares one")
        return sorted(out, key=lambda m: m.id)
