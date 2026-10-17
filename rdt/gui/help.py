"""The Help menu's windows and the update check (INS-7, INS-9, INS-12, INS-13).

``DocDialog`` shows a shipped Markdown file (user manual, release notes, licence); ``about`` shows
the version; ``UpdateCheck`` asks GitHub for the latest release off the GUI thread and hands the
answer back as a signal.
"""

from __future__ import annotations

import platform
import sys
import threading

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox, QTextBrowser, QVBoxLayout

from .. import appfiles, updates
from .._version import __version__


class DocDialog(QDialog):
    """A shipped Markdown file in a read-only window; links open in the browser."""

    def __init__(self, parent, title: str, name: str, size=(760, 640)):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        path = appfiles.doc_path(name)
        self.browser.setSearchPaths([str(path.parent)])   # the manual's images/ folder
        text = appfiles.read_doc(name)
        if text is None:
            self.browser.setPlainText(f"{path.name} is not part of this installation.")
        elif path.suffix == ".md":
            self.browser.setMarkdown(text)
        else:
            self.browser.setPlainText(text)
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setAttribute(Qt.WA_DeleteOnClose)


def about_text() -> str:
    return (f"<h3>RDT {__version__}</h3>"
            "<p>Remote Debugger Tool: read and write firmware variables and memory over a COM port or LAN.</p>"
            f"<p>Python {platform.python_version()}, PySide6 {pyside_version}, {platform.system()} {platform.release()}"
            f"{' (installed)' if getattr(sys, 'frozen', False) else ''}</p>"
            f'<p><a href="{updates.RELEASES_URL}">Releases on GitHub</a> · MIT licence</p>')


def about(parent) -> QMessageBox:
    box = QMessageBox(parent)
    box.setWindowTitle("About RDT")
    box.setTextFormat(Qt.RichText)
    box.setText(about_text())
    box.setStandardButtons(QMessageBox.Close)
    box.setAttribute(Qt.WA_DeleteOnClose)
    box.show()
    return box


class UpdateCheck(QObject):
    """Runs ``updates.fetch_latest`` on a thread; ``done`` carries the Release, or None when GitHub
    couldn't be read."""

    done = Signal(object)

    def start(self) -> None:
        threading.Thread(target=lambda: self.done.emit(updates.fetch_latest()), daemon=True,
                         name="rdt-update-check").start()
