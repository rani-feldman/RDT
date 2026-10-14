"""Drag and drop of watch expressions (GUI-16): from the variables list or a watch tree onto a
watch tab, a tab's title, or the chart."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QMimeData, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

MIME = "application/x-rdt-expressions"
PATH_ROLE = Qt.UserRole


def mime_for(exprs: list[str]) -> QMimeData:
    data = QMimeData()
    text = "\n".join(exprs)
    data.setData(MIME, text.encode())
    data.setText(text)
    return data


def exprs_from(data: QMimeData) -> list[str]:
    if not data.hasFormat(MIME):
        return []
    return [e for e in bytes(data.data(MIME)).decode().splitlines() if e.strip()]


class DragTree(QTreeWidget):
    """A tree whose rows can be dragged; each row carries its expression in PATH_ROLE."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)

    def mimeTypes(self) -> list[str]:
        return [MIME, "text/plain"]

    def mimeData(self, items) -> QMimeData:
        return mime_for([p for i in items if (p := i.data(0, PATH_ROLE))])


class DropTarget(QObject):
    """Accepts dropped expressions on a widget and passes them to ``on_drop``. ``on_hover`` is
    called while a drag moves over it, with the position; ``ignore`` names a source widget whose
    drags are refused (a tree dropping onto itself)."""

    def __init__(self, widget, on_drop: Callable[[list[str]], None], on_hover: Callable | None = None,
                 ignore=None):
        super().__init__(widget)
        self.on_drop, self.on_hover, self.ignore = on_drop, on_hover, ignore
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        kind = ev.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            if not ev.mimeData().hasFormat(MIME) or (self.ignore is not None and ev.source() is self.ignore):
                ev.ignore()
                return True
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            if kind == QEvent.DragMove and self.on_hover is not None:
                self.on_hover(ev.position().toPoint())
            if kind == QEvent.Drop:
                self.on_drop(exprs_from(ev.mimeData()))
            return True
        return False
