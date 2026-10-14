"""RDT's own, taller title bar (GUI-17): app icon and window title on the left, the menus after
them, and the minimise / maximise / close buttons on the right, all in one 48 px row. Windows fixes
the height of its standard title bar, so the window is frameless and this row replaces it.
Dragging, double-click to maximise and edge resizing use the system's own move and resize.
Set RDT_NATIVE_TITLEBAR=1 to keep the standard Windows title bar."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QToolButton, QWidget

BAR_HEIGHT = 48
ICON_SIZE = 32
GRIP = 6                                   # px of the window edge that resizes
ICON_FILE = Path(__file__).parent / "assets" / "rdt.png"


def enabled() -> bool:
    return os.environ.get("RDT_NATIVE_TITLEBAR", "") not in ("1", "true", "yes")


def _button(text: str, tip: str, close: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setFixedSize(52, BAR_HEIGHT)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.NoFocus)
    hover = "rgba(232,17,35,230)" if close else "rgba(128,128,128,70)"
    b.setStyleSheet(
        f"QToolButton {{ border: none; font-size: 15pt; background: transparent; }}"
        f"QToolButton:hover {{ background: {hover}; {'color: white;' if close else ''} }}")
    return b


class _Edges(QObject):
    """Moves the window by its menu row and resizes it by its edges."""

    def __init__(self, win: QMainWindow, bar: QWidget, maximise: QToolButton):
        super().__init__(win)
        self.win, self.bar, self.maximise = win, bar, maximise

    def _edges(self, pos: QPoint) -> Qt.Edges:
        if self.win.isMaximized() or self.win.isFullScreen():
            return Qt.Edges()
        e = Qt.Edges()
        if pos.x() <= GRIP:
            e |= Qt.LeftEdge
        if pos.x() >= self.win.width() - GRIP:
            e |= Qt.RightEdge
        if pos.y() <= GRIP:
            e |= Qt.TopEdge
        if pos.y() >= self.win.height() - GRIP:
            e |= Qt.BottomEdge
        return e

    @staticmethod
    def _cursor(e: Qt.Edges):
        horiz, vert = bool(e & (Qt.LeftEdge | Qt.RightEdge)), bool(e & (Qt.TopEdge | Qt.BottomEdge))
        if horiz and vert:
            diag = (e & Qt.LeftEdge and e & Qt.TopEdge) or (e & Qt.RightEdge and e & Qt.BottomEdge)
            return Qt.SizeFDiagCursor if diag else Qt.SizeBDiagCursor
        return Qt.SizeHorCursor if horiz else Qt.SizeVerCursor if vert else None

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self.bar:                      # the menu row: empty parts drag the window
            if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton \
                    and self.bar.actionAt(ev.position().toPoint()) is None:
                if self._edges(self.bar.mapTo(self.win, ev.position().toPoint())):
                    return False
                self.win.windowHandle().startSystemMove()
                return True
            if t == QEvent.MouseButtonDblClick and ev.button() == Qt.LeftButton \
                    and self.bar.actionAt(ev.position().toPoint()) is None:
                toggle_maximized(self.win)
                return True
            return False
        # the window itself: its maximised state and its thin edge
        if t == QEvent.WindowStateChange:
            maxed = self.win.isMaximized()
            self.maximise.setText("\u2750" if maxed else "\u2610")
            self.maximise.setToolTip("Restore" if maxed else "Maximise")
            m = 0 if maxed else GRIP // 2
            self.win.setContentsMargins(m, 0, m, m)
        elif t == QEvent.MouseMove and not ev.buttons():
            c = self._cursor(self._edges(ev.position().toPoint()))
            self.win.setCursor(c) if c is not None else self.win.unsetCursor()
        elif t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            e = self._edges(ev.position().toPoint())
            if e:
                self.win.windowHandle().startSystemResize(e)
                return True
        return False


def toggle_maximized(win: QMainWindow) -> None:
    win.showNormal() if win.isMaximized() else win.showMaximized()


def install(win: QMainWindow) -> None:
    """Replace win's standard title bar with the 48 px row. Call after the menus are built."""
    if not enabled():
        return
    win.setWindowFlag(Qt.FramelessWindowHint, True)
    win.setMouseTracking(True)
    win.setContentsMargins(GRIP // 2, 0, GRIP // 2, GRIP // 2)
    bar = win.menuBar()
    bar.setMinimumHeight(BAR_HEIGHT)
    bar.setStyleSheet(f"QMenuBar {{ font-size: 11pt; }} QMenuBar::item {{ padding: 6px 12px; }}")

    left = QWidget()
    lay = QHBoxLayout(left)
    lay.setContentsMargins(10, 0, 14, 0)
    lay.setSpacing(10)
    icon = QLabel()
    icon.setPixmap(QPixmap(str(ICON_FILE)).scaled(ICON_SIZE, ICON_SIZE, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation))
    title = QLabel(win.windowTitle())
    title.setStyleSheet("font-size: 12pt;")
    win.windowTitleChanged.connect(title.setText)
    lay.addWidget(icon)
    lay.addWidget(title)
    bar.setCornerWidget(left, Qt.TopLeftCorner)

    right = QWidget()
    rl = QHBoxLayout(right)
    rl.setContentsMargins(0, 0, 0, 0)
    rl.setSpacing(0)
    mn = _button("–", "Minimise")
    mx = _button("☐", "Maximise")
    cl = _button("✕", "Close", close=True)
    mn.clicked.connect(win.showMinimized)
    mx.clicked.connect(lambda: toggle_maximized(win))
    cl.clicked.connect(win.close)
    for b in (mn, mx, cl):
        rl.addWidget(b)
    bar.setCornerWidget(right, Qt.TopRightCorner)

    edges = _Edges(win, bar, mx)
    bar.installEventFilter(edges)
    win.installEventFilter(edges)
    win._titlebar_edges = edges               # keep alive
    title.setText(win.windowTitle() or "RDT")
