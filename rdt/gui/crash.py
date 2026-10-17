"""The GUI's log file and its handler for unexpected errors (INS-11).

The installed rdt-gui.exe has no console, so warnings and errors go to
``%LOCALAPPDATA%\\rdt\\logs\\rdt.log`` (kept to 3 files of 1 MB), and an error nothing caught is
written there with its traceback and shown in a dialog instead of closing the window.
"""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..console import history_path

log = logging.getLogger("rdt")


def log_path() -> Path:
    return history_path().parent / "logs" / "rdt.log"


def install(path: Path | None = None) -> Path | None:
    """Start the log file and catch unexpected errors. Returns the log file, or None when it can't
    be written (the errors are still shown)."""
    path = path or log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError:
        path = None
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.WARNING)
        logging.getLogger().addHandler(handler)
    sys.excepthook = lambda *exc: _unexpected(path, *exc)
    return path


def _unexpected(path: Path | None, kind, value, tb) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, tb)
        return
    log.error("unexpected error", exc_info=(kind, value, tb))
    text = "".join(traceback.format_exception_only(kind, value)).strip()
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            where = f"\n\nThe details are in {path}" if path else ""
            QMessageBox.critical(QApplication.activeWindow(), "RDT: unexpected error",
                                 f"Something went wrong that RDT did not expect:\n\n{text}{where}")
            return
    except Exception:
        pass
    sys.__excepthook__(kind, value, tb)
