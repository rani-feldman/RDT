"""RDT's own title bar (GUI-17): the app icon, the menus and the minimise / maximise / close
buttons, all in one row (no title text). Windows fixes
the height of its standard title bar, so the window is frameless and this row replaces it.
Dragging, double-click to maximise and edge resizing use the system's own move and resize.
Set RDT_NATIVE_TITLEBAR=1 to keep the standard Windows title bar."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QMenuBar, QToolButton, QWidget

BAR_HEIGHT = 34
MENU_POINT_SIZE = 10
ICON_SIZE = 22
GRIP = 6                                   # px of the window edge that resizes
ICON_FILE = Path(__file__).parent / "assets" / "rdt.png"


def enabled() -> bool:
    return os.environ.get("RDT_NATIVE_TITLEBAR", "") not in ("1", "true", "yes")


def _button(text: str, tip: str, close: bool = False) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setFixedSize(46, BAR_HEIGHT)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.NoFocus)
    hover = "rgba(232,17,35,230)" if close else "rgba(128,128,128,70)"
    b.setStyleSheet(
        f"QToolButton {{ border: none; font-size: 10pt; background: transparent; color: palette(window-text); }}"
        f"QToolButton:hover {{ background: {hover}; {'color: white;' if close else ''} }}")
    return b


class _Edges(QObject):
    """Resizes the window by its thin edge and keeps the maximise button in step."""

    def __init__(self, win: QMainWindow, maximise: QToolButton):
        super().__init__(win)
        self.win, self.maximise = win, maximise

    def _edges(self, pos: QPoint) -> Qt.Edges:
        if self.win.isMaximized() or self.win.isFullScreen():
            return Qt.Edge(0)
        e = Qt.Edge(0)
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


class _Row(QWidget):
    """The 48 px row. Empty parts of it (and its icon and title) move the window."""

    def __init__(self, win: QMainWindow):
        super().__init__()
        self.win = win
        self.setFixedHeight(BAR_HEIGHT)
        self.setAutoFillBackground(True)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self.win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            toggle_maximized(self.win)


def install(win: QMainWindow) -> QMenuBar:
    """Give win the one-row title bar and return the menu bar inside it. Call before the menus
    are built, and add the menus to the returned bar (not to win.menuBar())."""
    bar = QMenuBar()
    if not enabled():
        win.setMenuBar(bar)
        return bar
    win.setWindowFlag(Qt.FramelessWindowHint, True)
    win.setMouseTracking(True)
    win.setContentsMargins(GRIP // 2, 0, GRIP // 2, GRIP // 2)

    row = _Row(win)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(10, 0, 0, 0)
    lay.setSpacing(10)
    icon = QLabel()
    icon.setPixmap(QPixmap(str(ICON_FILE)).scaled(ICON_SIZE, ICON_SIZE, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation))
    # Menu items sit at the top of a QMenuBar, so pad them to the row height to centre the text.
    font = QFont(bar.font())
    font.setPointSize(MENU_POINT_SIZE)
    pad = max(0, (BAR_HEIGHT - QFontMetrics(font).height()) // 2)
    bar.setStyleSheet(f"QMenuBar {{ font-size: {MENU_POINT_SIZE}pt; background: transparent; color: palette(window-text); }}"
                      f"QMenuBar::item {{ padding: {pad}px 9px; background: transparent; color: palette(window-text); }}"
                      "QMenuBar::item:selected { background: rgba(128,128,128,70); }")
    lay.addWidget(icon)
    lay.addSpacing(4)
    lay.addWidget(bar)
    lay.addStretch(1)
    mn = _button("\u2013", "Minimise")
    mx = _button("\u2610", "Maximise")
    cl = _button("\u2715", "Close", close=True)
    mn.clicked.connect(win.showMinimized)
    mx.clicked.connect(lambda: toggle_maximized(win))
    cl.clicked.connect(win.close)
    for b in (mn, mx, cl):
        lay.addWidget(b)
    win.setMenuWidget(row)

    edges = _Edges(win, mx)
    win.installEventFilter(edges)
    win._titlebar_edges = edges               # keep alive
    return bar
