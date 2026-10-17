"""MCU tags (GUI-12), the change highlight (GUI-13) and the themes (GUI-15, LNF-1): the coloured
short name drawn before a watched item's name, the colour of a value that just changed, and the
window's colours, button shapes and row density. Also the line icons of the icon buttons (LNF-3)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate

from ..target import McuConfig, mcu_color, mcu_short

TAG_ROLE = Qt.UserRole + 1      # (short name, colour) on a row that shows its MCU as a tag
FLASH_S = 1.0                   # how long a changed value stays highlighted


def split_expr(expr: str) -> tuple[str, str]:
    """``Control_CM7:motor.speed`` is ("Control_CM7", "motor.speed"); ``@m:0x20000000:u32`` is
    ("m", "0x20000000:u32"). Without an MCU, the MCU is ""."""
    mcu, sep, rest = expr.lstrip("@").partition(":")
    return (mcu, rest) if sep and mcu and rest else ("", expr)


def mcu_tag(mcus: list[McuConfig], name: str) -> tuple[str, str] | None:
    for m in mcus:
        if m.name == name:
            return mcu_short(m), mcu_color(m)
    return None


@dataclass(frozen=True)
class Theme:
    """How the window looks (GUI-15, LNF-1): colours, button shapes and a few layout choices."""
    label: str
    palette: dict | None = None       # None: Fusion's standard light palette
    qss: str = ""                     # the application style sheet
    chart: tuple[str, str, str] | None = None   # plot background, axis line, axis text
    flash: str | None = None          # a value that just changed
    connect: dict | None = None       # the connect button's (text, border, background) per link look
    strip: dict | None = None         # the status bar's (background, text) per link look
    modes: str = ""                   # the panel buttons' style sheet
    panels_left: bool = False         # the panel buttons as an icon strip at the left edge


def _pal(win, base, alt, text, dim, button, highlight, highlighted, mid, tip=None) -> dict:
    return {QPalette.Window: win, QPalette.WindowText: text, QPalette.Base: base, QPalette.AlternateBase: alt,
            QPalette.ToolTipBase: tip or button, QPalette.ToolTipText: text, QPalette.Text: text,
            QPalette.Button: button, QPalette.ButtonText: text, QPalette.BrightText: "#ff6b6b",
            QPalette.Highlight: highlight, QPalette.HighlightedText: highlighted, QPalette.Link: highlight,
            QPalette.PlaceholderText: dim, QPalette.Light: mid, QPalette.Midlight: mid, QPalette.Mid: mid,
            QPalette.Dark: win, QPalette.Shadow: "#000000"}


# The status bar shows the link state as a coloured strip (LNF-5); "off" with no entry keeps the bar plain.
STRIP = {"up": ("#1f7a46", "#ffffff"), "busy": ("#8a6418", "#ffffff"), "down": ("#b3261e", "#ffffff")}
MODES = ("QToolButton { padding: 3px 12px; border: 1px solid palette(mid); background: palette(base); }"
         "QToolButton:checked { background: palette(highlight); color: palette(highlighted-text); font-weight: bold; }"
         "QToolButton#first { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }"
         "QToolButton#last { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }")

DARK = _pal("#202226", "#1a1b1e", "#222428", "#dcdfe4", "#8b919a", "#2a2c31", "#2f5d9e", "#ffffff", "#2a2c31",
            "#2b2e34")
DARK.update({QPalette.Light: "#3a3d43", QPalette.Midlight: "#33363b", QPalette.Dark: "#151618",
             QPalette.Shadow: "#0b0c0d"})
# White, set in full: Fusion's own standard palette follows Windows, so with Windows in dark mode
# it is dark, and otherwise it is grey (rani, 2026-10-17: "The light theme is not white").
LIGHT = _pal("#ffffff", "#ffffff", "#f5f6f7", "#1e1e1e", "#8a8f98", "#ffffff", "#cde3ff", "#1e1e1e", "#d0d3d8")
LIGHT.update({QPalette.Light: "#ffffff", QPalette.Midlight: "#e6e8eb", QPalette.Dark: "#a0a4aa",
              QPalette.Shadow: "#6b7078", QPalette.ToolTipBase: "#ffffdc", QPalette.Link: "#1a5fb4"})
LIGHT_CONNECT = {"up": ("#ffffff", "#1a7f37", "#1a7f37"), "busy": ("#1f2328", "#d4a72c", "#fff3c4"),
                 "down": ("#ffffff", "#cf222e", "#cf222e"), "off": ("#1a5fb4", "#1a5fb4", "#e8f0fc")}
LIGHT_DISABLED = {QPalette.WindowText: "#a0a4aa", QPalette.Text: "#a0a4aa", QPalette.ButtonText: "#a0a4aa"}
DARK_DISABLED = {QPalette.WindowText: "#6b7078", QPalette.Text: "#6b7078", QPalette.ButtonText: "#6b7078"}

THEME = {
    "system": Theme("&System", strip=STRIP, modes=MODES),
    "light": Theme("&Light", LIGHT, chart=("#ffffff", "#606060", "#404040"), flash="#fff1a8", strip=STRIP,
                   modes=MODES, connect=LIGHT_CONNECT),
    "dark": Theme("&Dark", DARK, chart=("#1a1b1e", "#80858d", "#c8ccd2"), strip=STRIP, modes=MODES),
    # Like VS Code and Segger Ozone: flat, square, the panel buttons as icons at the left edge.
    "studio": Theme(
        "S&tudio dark", _pal("#252526", "#1e1e1e", "#232324", "#d4d4d4", "#808080", "#333337", "#094771", "#ffffff",
                             "#3c3c3c"),
        chart=("#1e1e1e", "#5a5a5a", "#a0a0a0"), flash="#5c4a0f",
        connect={"up": ("#ffffff", "#16825d", "#16825d"), "off": ("#ffffff", "#0e639c", "#0e639c"),
                 "down": ("#ffffff", "#c72e2e", "#c72e2e"), "busy": ("#1e1e1e", "#cca700", "#cca700")},
        strip={"up": ("#16825d", "#ffffff"), "busy": ("#cca700", "#1e1e1e"), "down": ("#c72e2e", "#ffffff"),
               "off": ("#007acc", "#ffffff")},
        panels_left=True,
        modes="QToolButton { padding: 8px; border: none; border-left: 2px solid transparent; background: #2c2c2c; }"
              "QToolButton:checked { border-left: 2px solid #ffffff; background: #37373d; }"
              "QToolButton:hover { background: #3a3a40; }",
        qss="""
QMainWindow::separator { background: #2b2b2b; width: 1px; height: 1px; }
QToolBar { background: #2d2d30; border: none; spacing: 4px; padding: 2px; }
QToolBar#modes { background: #2c2c2c; padding: 4px 0px; spacing: 2px; }
QDockWidget::title { background: #252526; padding: 5px 8px; color: #bbbbbb; }
QTabBar::tab { background: #2d2d2d; color: #969696; padding: 5px 14px; border: none; border-top: 2px solid transparent; }
QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; border-top: 2px solid #007acc; }
QHeaderView::section { background: #252526; color: #bbbbbb; border: none; border-right: 1px solid #333333; padding: 3px 6px; }
QPushButton { background: #3a3d41; color: #e0e0e0; border: none; border-radius: 2px; padding: 4px 12px; }
QPushButton:hover { background: #45494e; }
QPushButton:checked { background: #094771; }
QLineEdit, QSpinBox, QComboBox { background: #3c3c3c; border: 1px solid #3c3c3c; border-radius: 2px; padding: 2px 4px; color: #cccccc; }
QTreeView, QTableView { border: none; }
"""),
    # Like the JetBrains new UI and Windows 11: rounded panels with gaps, one blue accent.
    "islands": Theme(
        "&Islands", _pal("#131418", "#1e1f22", "#222327", "#dfe1e5", "#868a91", "#2b2d30", "#2e436e", "#ffffff",
                         "#393b40"),
        chart=("#1e1f22", "#4a4d53", "#9da0a8"), flash="#5a4c12",
        connect={"up": ("#57d38c", "#2a5c40", "#1c3528"), "off": ("#8fb4ff", "#34508a", "#1d2a45"),
                 "down": ("#ff8a80", "#7a2f2f", "#3a1a1a"), "busy": ("#f2c057", "#6e5520", "#33290f")},
        strip={"up": ("#1c3528", "#57d38c"), "busy": ("#33290f", "#f2c057"), "down": ("#3a1a1a", "#ff8a80")},
        modes="QToolButton { padding: 5px 14px; border: none; background: #1e1f22; color: #9da0a8; }"
              "QToolButton:checked { background: #3574f0; color: white; font-weight: 600; }"
              "QToolButton#first { border-top-left-radius: 8px; border-bottom-left-radius: 8px; }"
              "QToolButton#last { border-top-right-radius: 8px; border-bottom-right-radius: 8px; }",
        qss="""
QMainWindow::separator { background: #131418; width: 8px; height: 8px; }
QToolBar { background: #131418; border: none; spacing: 6px; padding: 4px 8px; }
QDockWidget::title { background: #1e1f22; padding: 8px 12px; border-top-left-radius: 10px; border-top-right-radius: 10px; }
QTabWidget::pane { background: #1e1f22; border: none; border-radius: 10px; }
QTabBar::tab { background: transparent; color: #9da0a8; padding: 6px 14px; margin: 4px 2px; border-radius: 7px; }
QTabBar::tab:selected { background: #2b2d30; color: #ffffff; }
QHeaderView::section { background: #1e1f22; color: #868a91; border: none; border-bottom: 1px solid #2b2d30; padding: 5px 8px; font-weight: 600; }
QPushButton { background: #2b2d30; color: #dfe1e5; border: 1px solid #393b40; border-radius: 7px; padding: 5px 14px; }
QPushButton:hover { background: #33353a; }
QPushButton:checked { background: #3574f0; border-color: #3574f0; color: #ffffff; }
QLineEdit, QSpinBox, QComboBox { background: #1e1f22; border: 1px solid #393b40; border-radius: 7px; padding: 3px 7px; }
QTreeView, QTableView { border: none; }
QStatusBar { background: #131418; }
"""),
    # Like an oscilloscope or Saleae Logic: black plot, bright traces, bold square keys.
    "instrument": Theme(
        "I&nstrument", _pal("#0e1013", "#07080a", "#0b0c0f", "#e6e6e6", "#6c727c", "#16191e", "#1b3a2a", "#ffffff",
                            "#262a31"),
        chart=("#000000", "#3a3f47", "#8a9099"), flash="#4a4000",
        connect={"up": ("#000000", "#39ff88", "#39ff88"), "off": ("#cfd6e0", "#4b5563", "#16191e"),
                 "down": ("#ffffff", "#ff3b3b", "#ff3b3b"), "busy": ("#000000", "#ffb000", "#ffb000")},
        strip={"up": ("#0f2e1c", "#39ff88"), "busy": ("#2e2300", "#ffb000"), "down": ("#3a0d0d", "#ff6b6b")},
        modes="QToolButton { padding: 4px 14px; border: 1px solid #333943; background: #16191e; color: #8a9099;"
              " font-weight: bold; }"
              "QToolButton:checked { background: #22262d; color: #39ff88; border-bottom: 2px solid #39ff88; }",
        qss="""
QMainWindow::separator { background: #000000; width: 2px; height: 2px; }
QToolBar { background: #16191e; border: none; border-bottom: 1px solid #262a31; spacing: 6px; padding: 3px; }
QDockWidget::title { background: #16191e; padding: 4px 8px; color: #8a9099; }
QTabBar::tab { background: #16191e; color: #8a9099; padding: 5px 14px; border: none; border-bottom: 2px solid transparent; font-weight: bold; }
QTabBar::tab:selected { color: #39ff88; border-bottom: 2px solid #39ff88; }
QHeaderView::section { background: #0e1013; color: #6c727c; border: none; border-bottom: 1px solid #262a31; padding: 3px 6px; font-weight: bold; }
QPushButton { background: #1d2127; color: #d0d4da; border: 1px solid #333943; border-radius: 3px; padding: 4px 12px; font-weight: bold; }
QPushButton:checked { border-color: #39ff88; color: #39ff88; }
QLineEdit, QSpinBox, QComboBox { background: #07080a; border: 1px solid #333943; border-radius: 3px; padding: 2px 4px; }
QTreeView { border: none; }
QStatusBar { border-top: 1px solid #262a31; }
"""),
    # Like STM32CubeMonitor: white work area, navy title bar, light blue accent.
    "light_pro": Theme(
        "Light &pro", _pal("#f3f4f6", "#ffffff", "#f7f9fb", "#1f2328", "#6b7280", "#ffffff", "#d6ecfa", "#03234b",
                           "#d9dde3"),
        chart=("#ffffff", "#9ca3af", "#4b5563"), flash="#fff1a8",
        connect={"up": ("#ffffff", "#1a7f37", "#1a7f37"), "off": ("#03234b", "#3cb4e6", "#e6f5fc"),
                 "down": ("#ffffff", "#cf222e", "#cf222e"), "busy": ("#1f2328", "#d4a72c", "#fff3c4")},
        strip={"up": ("#1a7f37", "#ffffff"), "busy": ("#d4a72c", "#1f2328"), "down": ("#cf222e", "#ffffff")},
        modes="QToolButton { padding: 5px 16px; border: 1px solid #d0d7de; background: #ffffff; color: #374151; }"
              "QToolButton:checked { background: #03234b; color: white; font-weight: 600; border-color: #03234b; }"
              "QToolButton#first { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }"
              "QToolButton#last { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }",
        qss="""
QMainWindow::separator { background: #e5e7eb; width: 1px; height: 1px; }
QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #e5e7eb; spacing: 6px; padding: 4px 6px; }
QDockWidget::title { background: #ffffff; padding: 6px 10px; color: #03234b; border-bottom: 1px solid #e5e7eb; }
QTabBar::tab { background: transparent; color: #6b7280; padding: 6px 16px; border: none; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #03234b; border-bottom: 3px solid #3cb4e6; font-weight: 600; }
QHeaderView::section { background: #f7f9fb; color: #6b7280; border: none; border-bottom: 1px solid #e5e7eb; padding: 5px 8px; font-weight: 600; }
QPushButton { background: #ffffff; color: #1f2328; border: 1px solid #d0d7de; border-radius: 6px; padding: 5px 14px; }
QPushButton:hover { background: #f3f4f6; }
QPushButton:checked { background: #d6ecfa; border-color: #3cb4e6; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #d0d7de; border-radius: 6px; padding: 3px 7px; }
QTreeView { border: none; }
QStatusBar { border-top: 1px solid #e5e7eb; }
"""),
}
TITLE_BAR = {"light_pro": ("#03234b", "#ffffff")}   # (background, text) of a theme's own title bar row
THEMES = {name: t.label for name, t in THEME.items()}
DENSITY = {"compact": "&Compact rows", "comfortable": "Co&mfortable rows"}
_DENSITY_QSS = {"compact": "", "comfortable": "QTreeView::item, QTableView::item { padding: 3px 0px; }"}
_SYSTEM: tuple[str, QPalette] | None = None   # the style and palette Qt started with
_current = "system"
_density = "compact"


def apply_theme(name: str, density: str | None = None) -> str:
    """Colour the whole application: one of THEMES, where ``system`` is what Qt started with,
    which follows Windows. Returns the theme used; an unknown name is ``system``. ``density``
    (compact or comfortable rows, LNF-6) keeps the last one when not given."""
    global _SYSTEM, _current, _density
    app = QApplication.instance()
    if _SYSTEM is None:
        _SYSTEM = (app.style().name(), QPalette(app.palette()))
    if density in DENSITY:
        _density = density
    if name not in THEME:
        name = "system"
    _current = name
    t = THEME[name]
    if name == "system":
        app.setStyle(_SYSTEM[0])
        app.setPalette(_SYSTEM[1])
    else:
        app.setStyle("Fusion")
        if t.palette is None:
            app.setPalette(app.style().standardPalette())
        else:
            p = QPalette()
            for role, color in t.palette.items():
                p.setColor(role, QColor(color))
            dark = QColor(t.palette[QPalette.Base]).lightness() < 128
            for role, color in (DARK_DISABLED if dark else LIGHT_DISABLED).items():
                p.setColor(QPalette.Disabled, role, QColor(color))
            app.setPalette(p)
    app.setStyleSheet(t.qss + _DENSITY_QSS[_density])
    return name


def theme() -> Theme:
    """The theme in use."""
    return THEME[_current]


def theme_name() -> str:
    return _current


def density() -> str:
    return _density


def is_dark() -> bool:
    return QApplication.palette().base().color().lightness() < 128


def flash_color() -> QColor:
    if theme().flash:
        return QColor(theme().flash)
    return QColor("#5a4c12") if is_dark() else QColor("#fff1a8")


def line_icon(kind: str, color: str, size: int = 20) -> QIcon:
    """A small line-drawn icon for the icon buttons (LNF-3): watch, scope, memory, console,
    refresh, csv, gear, chart, remove."""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    q = QPainter(pm)
    q.setRenderHint(QPainter.Antialiasing)
    q.scale(2 * size / 22, 2 * size / 22)
    pen = QPen(QColor(color), 1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    q.setPen(pen)
    if kind == "watch":
        for k in range(3):
            y = 6 + k * 5
            q.drawPoint(QPointF(5, y))
            q.drawLine(QPointF(9, y), QPointF(18, y))
    elif kind in ("scope", "chart"):
        path = QPainterPath(QPointF(3, 11))
        for x in range(3, 20):
            path.lineTo(x, 11 - 6 * math.sin((x - 3) / 2.6))
        q.drawPath(path)
    elif kind == "memory":
        for i in range(3):
            for j in range(3):
                q.drawRect(QRectF(4 + i * 5.3, 4 + j * 5.3, 3.4, 3.4))
    elif kind == "console":
        q.drawLine(QPointF(4, 7), QPointF(9, 11))
        q.drawLine(QPointF(9, 11), QPointF(4, 15))
        q.drawLine(QPointF(11, 16), QPointF(18, 16))
    elif kind == "refresh":
        q.drawArc(QRectF(4, 4, 14, 14), 30 * 16, 290 * 16)
        q.drawLine(QPointF(17, 4), QPointF(17.5, 9))
        q.drawLine(QPointF(17.5, 9), QPointF(12.5, 9))
    elif kind == "csv":
        q.drawRoundedRect(QRectF(5, 3, 12, 16), 1.5, 1.5)
        for y in (8, 11.5, 15):
            q.drawLine(QPointF(8, y), QPointF(14, y))
    elif kind == "gear":
        q.drawEllipse(QRectF(7, 7, 8, 8))
        for a in range(0, 360, 45):
            r = math.radians(a)
            q.drawLine(QPointF(11 + 5.5 * math.cos(r), 11 + 5.5 * math.sin(r)),
                       QPointF(11 + 8.5 * math.cos(r), 11 + 8.5 * math.sin(r)))
    elif kind == "remove":
        q.drawLine(QPointF(6, 6), QPointF(16, 16))
        q.drawLine(QPointF(16, 6), QPointF(6, 16))
    q.end()
    return QIcon(pm)


FIXED_FAMILIES = ("Consolas", "Cascadia Mono", "Courier New", "DejaVu Sans Mono", "Liberation Mono")


def fixed_font() -> QFont:
    """The system's fixed-width font; when Qt can't name one that is really fixed (the family
    "monospace" on some platforms), the first fixed family that is installed."""
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setStyleHint(QFont.TypeWriter)
    if QFontInfo(f).fixedPitch():
        return f
    for family in FIXED_FAMILIES:
        g = QFont(f)
        g.setFamily(family)
        if QFontInfo(g).fixedPitch():
            return g
    return f


def mono_font(base: QFont) -> QFont:
    """The fixed-width font at ``base``'s size, for values whose digits must line up (LNF-8)."""
    f = fixed_font()
    if base.pointSizeF() > 0:
        f.setPointSizeF(base.pointSizeF())
    return f


def shade(color: str, k: int) -> str:
    """The k-th variant of an MCU's colour for its k-th chart trace (LNF-4): the colour itself,
    then lighter and darker turns."""
    if k == 0:
        return color
    c = QColor(color)
    step = (k + 1) // 2
    return (c.lighter(100 + 35 * step) if k % 2 else c.darker(100 + 30 * step)).name()


class TagDelegate(QStyledItemDelegate):
    """Draws a row's MCU tag, a rounded pill with the short name, before its text."""

    PAD = 4

    def paint(self, p: QPainter, opt, idx) -> None:
        tag = idx.data(TAG_ROLE)
        if not tag:
            super().paint(p, opt, idx)
            return
        short, color = tag
        font = QFont(opt.font)
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
        font.setBold(True)
        width = self._tag_width(font, short)
        self.initStyleOption(opt, idx)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, p, opt.widget)   # background and selection only
        r = opt.rect
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        pill = QRectF(r.left() + 2, r.top() + 3, width - 2, max(8, r.height() - 6))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(pill, 4, 4)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(pill, Qt.AlignCenter, short)
        p.setFont(opt.font)
        selected = opt.state & QStyle.State_Selected
        fg = opt.palette.highlightedText().color() if selected else opt.palette.text().color()
        brush = idx.data(Qt.ForegroundRole)
        if brush is not None and not selected:
            fg = brush.color()
        p.setPen(fg)
        rest = r.adjusted(width + self.PAD, 0, -2, 0)
        p.drawText(rest, Qt.AlignVCenter | Qt.AlignLeft, opt.fontMetrics.elidedText(text, Qt.ElideRight, rest.width()))
        p.restore()

    def sizeHint(self, opt, idx):
        size = super().sizeHint(opt, idx)
        tag = idx.data(TAG_ROLE)
        if tag:
            font = QFont(opt.font)
            font.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
            font.setBold(True)
            size.setWidth(size.width() + self._tag_width(font, tag[0]) + self.PAD)
        return size

    @staticmethod
    def _tag_width(font: QFont, text: str) -> int:
        from PySide6.QtGui import QFontMetrics
        return QFontMetrics(font).horizontalAdvance(text) + 12
