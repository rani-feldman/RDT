"""MCU tags (GUI-12), the change highlight (GUI-13) and the light and dark themes (GUI-14): the
coloured short name drawn before a watched item's name, the colour of a value that just changed,
and the window's colours."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
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


THEMES = {"system": "&System", "light": "&Light", "dark": "&Dark"}
_SYSTEM: tuple[str, QPalette] | None = None   # the style and palette Qt started with
DARK = {QPalette.Window: "#202226", QPalette.WindowText: "#dcdfe4", QPalette.Base: "#1a1b1e",
        QPalette.AlternateBase: "#222428", QPalette.ToolTipBase: "#2b2e34", QPalette.ToolTipText: "#dcdfe4",
        QPalette.Text: "#dcdfe4", QPalette.Button: "#2a2c31", QPalette.ButtonText: "#dcdfe4",
        QPalette.BrightText: "#ff6b6b", QPalette.Highlight: "#2f5d9e", QPalette.HighlightedText: "#ffffff",
        QPalette.Link: "#6ea8ff", QPalette.PlaceholderText: "#8b919a", QPalette.Light: "#3a3d43",
        QPalette.Midlight: "#33363b", QPalette.Mid: "#2a2c31", QPalette.Dark: "#151618", QPalette.Shadow: "#0b0c0d"}
DARK_DISABLED = {QPalette.WindowText: "#6b7078", QPalette.Text: "#6b7078", QPalette.ButtonText: "#6b7078"}


def apply_theme(name: str) -> str:
    """Colour the whole application: ``dark``, ``light``, or ``system`` (what Qt started with,
    which follows Windows). Returns the theme used; an unknown name is ``system``."""
    global _SYSTEM
    app = QApplication.instance()
    if _SYSTEM is None:
        _SYSTEM = (app.style().name(), QPalette(app.palette()))
    if name not in THEMES or name == "system":
        app.setStyle(_SYSTEM[0])
        app.setPalette(_SYSTEM[1])
        return "system"
    app.setStyle("Fusion")
    if name == "light":
        app.setPalette(app.style().standardPalette())
        return name
    p = QPalette()
    for role, color in DARK.items():
        p.setColor(role, QColor(color))
    for role, color in DARK_DISABLED.items():
        p.setColor(QPalette.Disabled, role, QColor(color))
    app.setPalette(p)
    return name


def is_dark() -> bool:
    return QApplication.palette().base().color().lightness() < 128


def flash_color() -> QColor:
    return QColor("#5a4c12") if is_dark() else QColor("#fff1a8")


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
