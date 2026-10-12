"""The console's terminal (CON-13, CON-14): one text area holding the transcript, whose last line
is the prompt you type on, with the best suggestion shown in grey after the cursor as you type."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

# keys that only look or move, never edit
_VIEW_KEYS = {Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta}


class Terminal(QPlainTextEdit):
    """A transcript you type into. Everything before the prompt's input is fixed; typing anywhere
    goes to the end of the last line. Enter sends that line (``returnPressed``), the transcript's
    new lines are inserted above the prompt, and a grey suggestion after the cursor is taken with
    Tab or Right."""

    returnPressed = Signal()
    textEdited = Signal(str)
    history_step = Signal(int)          # Up (-1) or Down (+1)
    complete_requested = Signal()       # Tab with no suggestion shown, or Ctrl+Space

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUndoRedoEnabled(False)
        self.setTabChangesFocus(False)
        self.prompt = ""
        self.prompt_color = "#3fb950"
        self.arrow_color = "#6e7681"
        self.text_color = "#c9d1d9"
        self.ghost_color = "#6e7681"
        self.hint = ""                  # shown in grey on an empty prompt
        self.suggestion = ""            # the whole command suggested for what is typed
        self.input_enabled = True
        self.popup_open = lambda: False  # set by the console: is the completer's list showing?
        self._input_start = 0
        self._write_prompt("")

    # --- the prompt line ---------------------------------------------------------------------

    def input_start(self) -> int:
        return self._input_start

    def text(self) -> str:
        """What is typed on the prompt line."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        return cursor.selectedText()

    def setText(self, text: str) -> None:
        text = text.replace("\n", " ").replace("\r", "")
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.insertText(text, self._input_format())
        self._to_end()
        self.viewport().update()

    def set_prompt(self, prompt: str, color: str | None = None) -> None:
        """Show ``prompt >`` at the start of the last line, keeping what is typed."""
        typed = self.text()
        self.prompt = prompt
        if color:
            self.prompt_color = color
        self._write_prompt(typed)

    def _write_prompt(self, typed: str) -> None:
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.setPosition(doc.lastBlock().position())
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.prompt_color))
        if self.prompt:
            cursor.insertText(self.prompt + " ", fmt)
        fmt.setForeground(QColor(self.arrow_color))
        cursor.insertText("> ", fmt)
        self._input_start = cursor.position()
        cursor.insertText(typed, self._input_format())
        self._to_end()

    def _input_format(self) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.text_color))
        return fmt

    def _to_end(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # --- the transcript ----------------------------------------------------------------------

    def insert_above(self) -> QTextCursor:
        """A cursor at the start of the prompt line, to insert transcript lines before it. The
        caller ends every line it adds with ``insertBlock()``; ``done_inserting`` fixes up after."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(self.document().lastBlock().position())
        return cursor

    def done_inserting(self, follow: bool) -> None:
        """After lines were inserted above the prompt: find the input again, and when ``follow``
        (the cursor was on the prompt line) keep the prompt in view."""
        last = self.document().lastBlock()
        self._input_start = last.position() + len(self.prompt + " " if self.prompt else "") + 2
        if follow:
            self.ensureCursorVisible()

    def clear_transcript(self) -> None:
        typed = self.text()
        self.document().clear()
        self._write_prompt(typed)

    # --- keys --------------------------------------------------------------------------------

    def _cursor_in_input(self) -> bool:
        c = self.textCursor()
        return min(c.position(), c.anchor()) >= self._input_start

    def keyPressEvent(self, event) -> None:
        key, mods = event.key(), event.modifiers()
        if event.matches(QKeySequence.Copy) or event.matches(QKeySequence.SelectAll):
            return super().keyPressEvent(event)
        if key in _VIEW_KEYS or (mods & Qt.ControlModifier and key in (Qt.Key_C, Qt.Key_A, Qt.Key_Home, Qt.Key_End)):
            return super().keyPressEvent(event)
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self.input_enabled:
                self.suggestion = ""
                self.returnPressed.emit()
            return
        if key in (Qt.Key_Up, Qt.Key_Down) and not mods & Qt.ShiftModifier:
            if self.input_enabled:
                self.history_step.emit(-1 if key == Qt.Key_Up else 1)
            return
        if (key == Qt.Key_Tab and not mods & ~Qt.KeypadModifier) or \
                (key == Qt.Key_Space and mods & Qt.ControlModifier):
            if self.input_enabled:
                if key == Qt.Key_Tab and self.accept_suggestion():
                    return
                self.complete_requested.emit()
            return
        if key == Qt.Key_Escape:
            self.suggestion = ""
            self.viewport().update()
            return
        at_end = self.textCursor().position() == self.document().characterCount() - 1
        if key in (Qt.Key_Right, Qt.Key_End) and at_end and not mods & Qt.ShiftModifier:
            if self.accept_suggestion():
                return
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End):
            if not self._cursor_in_input() or key == Qt.Key_Home:
                if key == Qt.Key_Home:
                    cursor = self.textCursor()
                    cursor.setPosition(self._input_start,
                                       QTextCursor.KeepAnchor if mods & Qt.ShiftModifier else QTextCursor.MoveAnchor)
                    self.setTextCursor(cursor)
                    return
                self._to_end()
            if key == Qt.Key_Left and self.textCursor().position() <= self._input_start \
                    and not self.textCursor().hasSelection():
                return
            return super().keyPressEvent(event)
        # anything else edits the prompt line
        if not self.input_enabled:
            return
        if not self._cursor_in_input():
            self._to_end()
        if key == Qt.Key_Backspace and self.textCursor().position() <= self._input_start \
                and not self.textCursor().hasSelection():
            return
        before = self.text()
        super().keyPressEvent(event)
        if self.text() != before:
            self.textEdited.emit(self.text())
        self.viewport().update()

    def insertFromMimeData(self, source) -> None:
        """Paste goes to the prompt line, as one line."""
        if not self.input_enabled or not source.hasText():
            return
        if not self._cursor_in_input():
            self._to_end()
        self.textCursor().insertText(" ".join(source.text().split("\n")).replace("\r", ""), self._input_format())
        self.textEdited.emit(self.text())

    def canInsertFromMimeData(self, source) -> bool:
        return source.hasText()

    # --- suggestion --------------------------------------------------------------------------

    def ghost(self) -> str:
        """The grey text after the cursor: the rest of the suggestion, or the hint."""
        typed = self.text()
        if not typed:
            return self.hint
        if self.suggestion and self.suggestion.casefold().startswith(typed.casefold()) \
                and len(self.suggestion) > len(typed):
            return self.suggestion[len(typed):]
        return ""

    def accept_suggestion(self) -> bool:
        """Take the suggestion shown; False when there is none."""
        typed = self.text()
        if not typed or not self.ghost():
            return False
        self.setText(self.suggestion)
        self.suggestion = ""
        self.textEdited.emit(self.text())
        return True

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        ghost = self.ghost()
        if not ghost or self.textCursor().position() != self.document().characterCount() - 1 \
                or self.popup_open():
            return
        rect = self.cursorRect()
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.setPen(QColor(self.ghost_color))
        fm = painter.fontMetrics()
        painter.drawText(rect.left() + 1, rect.top() + fm.ascent(), ghost)
        painter.end()
