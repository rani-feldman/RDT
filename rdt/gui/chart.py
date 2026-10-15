"""Live chart: an oscilloscope of watched values.

Any numeric value in the watch tree (a variable, a field or a cell) can be a signal. Each signal
keeps the last 10 minutes. The chart follows the newest data over a chosen time window until it
is paused, or until the user zooms or pans with the mouse. Two cursors read every signal's value
at two moments and the time between them. Signals sit on up to four Y axes; by default signals
with the same unit share one.

The layout (window, signals, their axes and colours) is kept in the workspace's ``[chart]``.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import numpy as np  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PySide6.QtCore import Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap, QShortcut  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QColorDialog, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..stats import numeric

HISTORY_S = 600
WINDOWS = (("1 s", 1), ("5 s", 5), ("10 s", 10), ("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300),
           ("10 min", 600))
DEFAULT_WINDOW_S = 10
MAX_AXES = 4
REDRAW_MS = 50
COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#17becf", "#8c564b", "#e377c2", "#7f7f7f",
          "#bcbd22")
COL_NAME, COL_AXIS, COL_VALUE, COL_C1, COL_C2, COL_DELTA = range(6)


def inside(path: str, expr: str) -> bool:
    """True when ``path`` is ``expr`` or one of its fields or cells."""
    return path == expr or path.startswith(expr) and path[len(expr)] in ".[-"


class Trace:
    """Time and value samples in two growing arrays; old samples are dropped from the front."""

    def __init__(self, capacity: int = 1024):
        self._t = np.empty(capacity)
        self._v = np.empty(capacity)
        self._a = self._b = 0

    def __len__(self) -> int:
        return self._b - self._a

    def add(self, t: float, v: float) -> None:
        if self._b == len(self._t):
            n = len(self)
            size = len(self._t) * 2 if n * 2 > len(self._t) else len(self._t)
            t_new, v_new = np.empty(size), np.empty(size)
            t_new[:n], v_new[:n] = self._t[self._a:self._b], self._v[self._a:self._b]
            self._t, self._v, self._a, self._b = t_new, v_new, 0, n
        self._t[self._b] = t
        self._v[self._b] = v
        self._b += 1

    def trim(self, before: float) -> None:
        self._a += int(np.searchsorted(self.t, before))

    def clear(self) -> None:
        self._a = self._b = 0

    @property
    def t(self) -> np.ndarray:
        return self._t[self._a:self._b]

    @property
    def v(self) -> np.ndarray:
        return self._v[self._a:self._b]

    def at(self, when: float) -> float | None:
        """The value at a moment: the last sample at or before it (NaN in a gap)."""
        i = int(np.searchsorted(self.t, when, side="right")) - 1
        return float(self.v[i]) if i >= 0 else None

    def last(self) -> float | None:
        v = self.v
        good = v[~np.isnan(v)]
        return float(good[-1]) if len(good) else None


class TimeAxis(pg.AxisItem):
    """Wall-clock time of day: 14:03:22, with milliseconds when zoomed in below a second."""

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            text = time.strftime("%H:%M:%S", time.localtime(v))
            if spacing < 1:
                text += f".{int(round(v * 1000)) % 1000:03d}"
            out.append(text)
        return out


@dataclass
class Series:
    path: str
    color: str
    axis: int = 1          # 1..MAX_AXES
    unit: str = ""
    visible: bool = True
    trace: Trace | None = None
    curve: pg.PlotDataItem | None = None
    item: QTreeWidgetItem | None = None


def _swatch(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(QColor(color))
    return QIcon(pm)


def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return ""
    if math.isnan(v):
        return "gap"
    return f"{v:.6g}" + (f" {unit}" if unit else "")


class ChartPanel(QWidget):
    changed = Signal()            # the chart's layout changed (signals, axes, colours, window)
    series_changed = Signal()     # the set of signals changed: the watch tree reads what they need

    def __init__(self, main=None):
        super().__init__()
        self.main = main
        self.series: dict[str, Series] = {}
        self.paused = False
        self.window_s = DEFAULT_WINDOW_S
        self._dirty = False
        self._last_trim = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Freeze the view; data keeps coming in (Space while the chart has focus)")
        self.pause_button.toggled.connect(self.set_paused)
        self.window = QComboBox()
        for text, s in WINDOWS:
            self.window.addItem(text, s)
        self.window.setToolTip("How much time the chart shows while it follows the newest data")
        self.window.currentIndexChanged.connect(self._window_changed)
        self.cursor_button = QPushButton("Cursors")
        self.cursor_button.setCheckable(True)
        self.cursor_button.setToolTip("Two cursors that read every signal at two moments")
        self.cursor_button.toggled.connect(self.show_cursors)
        fit = QPushButton("Fit Y")
        fit.setToolTip("Scale every Y axis to the data in view again")
        fit.clicked.connect(self.fit_y)
        clear = QPushButton("Clear")
        clear.setToolTip("Drop the recorded data; the signals stay")
        clear.clicked.connect(self.clear)
        self.readout = QLabel("")
        for w in (self.pause_button, QLabel("Window"), self.window, self.cursor_button, fit, clear):
            bar.addWidget(w)
        bar.addWidget(self.readout, 1)
        layout.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis("bottom")}, background="w")
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.25)
        pi.setMenuEnabled(False)
        pi.hideButtons()
        self.views = [pi.vb]
        self.axes = [pi.getAxis("left")]
        pi.showAxis("right")
        for k in range(1, MAX_AXES):
            vb = pg.ViewBox()
            axis = pi.getAxis("right") if k == 1 else pg.AxisItem("right")
            if k > 1:
                pi.layout.addItem(axis, 2, 2 + k - 1)
            pi.scene().addItem(vb)
            axis.linkToView(vb)
            vb.setXLink(pi.vb)
            self.views.append(vb)
            self.axes.append(axis)
        for vb in self.views:
            vb.setMouseEnabled(x=True, y=True)
            vb.enableAutoRange(axis=pg.ViewBox.YAxis)
            vb.setAutoVisible(y=True)
        pi.vb.disableAutoRange(axis=pg.ViewBox.XAxis)
        pi.vb.sigResized.connect(self._sync_views)
        pi.vb.sigRangeChangedManually.connect(lambda *_: self.pause_button.setChecked(True))
        self.axis_text = "#404040"
        self.set_dark(False)
        self.cursors = []
        for name, color in (("C1", "#c03030"), ("C2", "#3050c0")):
            line = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen(color, width=1, style=Qt.DashLine),
                                   label=name, labelOpts={"position": 0.95, "color": color})
            line.setVisible(False)
            line.sigPositionChanged.connect(self._update_values)
            pi.addItem(line, ignoreBounds=True)
            self.cursors.append(line)
        split.addWidget(self.plot)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Signal", "Axis", "Value", "C1", "C2", "C2 - C1"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        for c, w in ((COL_NAME, 170), (COL_AXIS, 40), (COL_VALUE, 100), (COL_C1, 80), (COL_C2, 80), (COL_DELTA, 80)):
            self.list.setColumnWidth(c, w)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemChanged.connect(self._item_changed)
        for c in (COL_C1, COL_C2, COL_DELTA):
            self.list.setColumnHidden(c, True)
        split.addWidget(self.list)
        split.setStretchFactor(0, 1)
        split.setSizes([700, 360])
        layout.addWidget(split, 1)
        self.hint = QLabel("Right-click a value in the watch tree and choose Add to chart.")
        layout.addWidget(self.hint)

        space = QShortcut(QKeySequence(Qt.Key_Space), self, lambda: self.set_paused(not self.paused))
        space.setContext(Qt.WidgetWithChildrenShortcut)
        self.window.setCurrentIndex([s for _, s in WINDOWS].index(DEFAULT_WINDOW_S))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.redraw)
        self.timer.start(REDRAW_MS)
        self._update_axes()

    # --- layout ------------------------------------------------------------------------------

    def _sync_views(self) -> None:
        main = self.views[0]
        for vb in self.views[1:]:
            vb.setGeometry(main.sceneBoundingRect())
            vb.linkedViewChanged(main, vb.XAxis)

    def has(self, path: str) -> bool:
        return path in self.series

    def wants(self, expr: str) -> bool:
        """True when a signal is ``expr`` or one of its fields: the item must be read even when its
        row is collapsed."""
        return any(inside(p, expr) for p in self.series)

    def add(self, path: str, unit: str = "", axis: int | None = None, color: str | None = None,
            visible: bool = True, quiet: bool = False) -> Series:
        if path in self.series:
            return self.series[path]
        if axis is None:
            axis = self._axis_for(unit)
        if color is None:
            used = {s.color for s in self.series.values()}
            color = next((c for c in COLORS if c not in used), COLORS[len(self.series) % len(COLORS)])
        s = Series(path, color, max(1, min(MAX_AXES, axis)), unit, visible, Trace())
        s.curve = pg.PlotDataItem(pen=pg.mkPen(color, width=1.5), connect="finite")
        s.curve.setClipToView(True)
        s.curve.setDownsampling(auto=True, method="peak")
        s.curve.setVisible(visible)
        self.views[s.axis - 1].addItem(s.curve)
        s.item = QTreeWidgetItem([path, str(s.axis), "", "", "", ""])
        s.item.setIcon(COL_NAME, _swatch(color))
        s.item.setFlags(s.item.flags() | Qt.ItemIsUserCheckable)
        s.item.setCheckState(COL_NAME, Qt.Checked if visible else Qt.Unchecked)
        s.item.setData(COL_NAME, Qt.UserRole, path)
        self.list.blockSignals(True)
        self.list.addTopLevelItem(s.item)
        self.list.blockSignals(False)
        self.series[path] = s
        self._update_axes()
        if not quiet:
            self.series_changed.emit()
            self.changed.emit()
        return s

    def remove(self, path: str) -> None:
        s = self.series.pop(path, None)
        if s is None:
            return
        self.views[s.axis - 1].removeItem(s.curve)
        self.list.takeTopLevelItem(self.list.indexOfTopLevelItem(s.item))
        self._update_axes()
        self.series_changed.emit()
        self.changed.emit()

    def set_axis(self, path: str, axis: int) -> None:
        s = self.series[path]
        if axis == s.axis:
            return
        self.views[s.axis - 1].removeItem(s.curve)
        s.axis = axis
        self.views[axis - 1].addItem(s.curve)
        s.item.setText(COL_AXIS, str(axis))
        self._update_axes()
        self.changed.emit()

    def set_color(self, path: str, color: str) -> None:
        s = self.series[path]
        s.color = color
        s.curve.setPen(pg.mkPen(color, width=1.5))
        s.item.setIcon(COL_NAME, _swatch(color))
        self._update_axes()
        self.changed.emit()

    def _axis_for(self, unit: str) -> int:
        """The axis of the signals with this unit, else the first axis no signal uses."""
        for s in self.series.values():
            if s.unit == unit:
                return s.axis
        used = {s.axis for s in self.series.values()}
        return next((a for a in range(1, MAX_AXES + 1) if a not in used), MAX_AXES)

    def set_dark(self, dark: bool) -> None:
        """Plot colours for the dark or the light theme (GUI-15)."""
        self.plot.setBackground("#1a1b1e" if dark else "w")
        line, self.axis_text = ("#80858d", "#c8ccd2") if dark else ("#606060", "#404040")
        for axis in self.axes + [self.plot.getAxis("bottom")]:
            axis.setPen(pg.mkPen(line))
            axis.setTextPen(pg.mkPen(self.axis_text))
        if hasattr(self, "hint"):   # built: label the axes in the new colour
            self._update_axes()

    def set_colors(self, background: str, line: str, text: str) -> None:
        """A theme's own plot colours (LNF-1)."""
        self.plot.setBackground(background)
        self.axis_text = text
        for axis in self.axes + [self.plot.getAxis("bottom")]:
            axis.setPen(pg.mkPen(line))
            axis.setTextPen(pg.mkPen(text))
        if hasattr(self, "hint"):
            self._update_axes()

    def _update_axes(self) -> None:
        for k, axis in enumerate(self.axes, start=1):
            on = [s for s in self.series.values() if s.axis == k]
            if k > 1:
                axis.setVisible(bool(on))
            units = sorted({s.unit for s in on if s.unit})
            label = ", ".join(units) if units else (on[0].path if len(on) == 1 else "")
            axis.setLabel(label, color=on[0].color if on else self.axis_text)
        self.hint.setVisible(not self.series)

    def _window_changed(self) -> None:
        s = self.window.currentData()
        if s and s != self.window_s:
            self.window_s = s
            self.changed.emit()
        self._dirty = True

    def to_dict(self) -> dict:
        d: dict = {"window_s": self.window_s}
        series = []
        for s in self.series.values():
            e: dict = {"expr": s.path, "axis": s.axis, "color": s.color}
            if s.unit:
                e["unit"] = s.unit
            if not s.visible:
                e["hidden"] = True
            series.append(e)
        if series:
            d["series"] = series
        return d

    def load(self, d: dict) -> None:
        """Show the layout kept in a workspace's ``[chart]``. Unknown keys are ignored."""
        for path in list(self.series):
            s = self.series.pop(path)
            self.views[s.axis - 1].removeItem(s.curve)
        self.list.clear()
        window = d.get("window_s", DEFAULT_WINDOW_S)
        values = [s for _, s in WINDOWS]
        self.window.blockSignals(True)
        self.window.setCurrentIndex(values.index(window) if window in values else values.index(DEFAULT_WINDOW_S))
        self.window.blockSignals(False)
        self.window_s = self.window.currentData()
        for e in d.get("series", []) if isinstance(d.get("series"), list) else []:
            if isinstance(e, dict) and isinstance(e.get("expr"), str):
                axis = e.get("axis") if isinstance(e.get("axis"), int) else None
                color = e.get("color") if isinstance(e.get("color"), str) and QColor(e["color"]).isValid() else None
                self.add(e["expr"], str(e.get("unit", "")), axis, color, not e.get("hidden", False), quiet=True)
        self.set_paused(False)
        self.series_changed.emit()

    # --- data --------------------------------------------------------------------------------

    def feed(self, readings) -> None:
        """Take one poll's readings (``rdt.workspace.Reading``) from the watch tree."""
        if not self.series:
            return
        done = set()
        for r in readings:
            if r.error is not None:
                gap = [s for p, s in self.series.items() if inside(p, r.watch.expr) and p not in done]
                for s in gap:
                    if len(s.trace) and not math.isnan(s.trace.v[-1]):
                        s.trace.add(r.time, math.nan)   # breaks the line: a gap in the chart
                continue
            for row in r.shown or ():
                s = self.series.get(row.path)
                if s is None or row.path in done or row.text is None:
                    continue
                v = numeric(row.value)
                if v is None:
                    continue
                s.trace.add(r.time, v)
                done.add(row.path)
        if done:
            self._dirty = True

    def clear(self) -> None:
        for s in self.series.values():
            s.trace.clear()
        self._dirty = True

    # --- view --------------------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if self.pause_button.isChecked() != paused:
            self.pause_button.blockSignals(True)
            self.pause_button.setChecked(paused)
            self.pause_button.blockSignals(False)
        self.pause_button.setText("Run" if paused else "Pause")
        if not paused:
            self.fit_y()
        self._dirty = True

    def fit_y(self) -> None:
        for vb in self.views:
            vb.enableAutoRange(axis=pg.ViewBox.YAxis)

    def show_cursors(self, on: bool) -> None:
        if on:
            (x0, x1), _ = self.views[0].viewRange()
            for line, f in zip(self.cursors, (1 / 3, 2 / 3)):
                if not x0 <= line.value() <= x1:
                    line.setValue(x0 + f * (x1 - x0))
        for line in self.cursors:
            line.setVisible(on)
        for c in (COL_C1, COL_C2, COL_DELTA):
            self.list.setColumnHidden(c, not on)
        self._update_values()

    def redraw(self) -> None:
        now = time.time()
        if now - self._last_trim > 1.0:
            self._last_trim = now
            for s in self.series.values():
                s.trace.trim(now - HISTORY_S)
        if not self.paused and self.series:
            self.views[0].setXRange(now - self.window_s, now, padding=0)
        if not self._dirty:
            return
        self._dirty = False
        for s in self.series.values():
            if s.visible:
                s.curve.setData(s.trace.t, s.trace.v)
        self._update_values()

    def _update_values(self) -> None:
        on = self.cursor_button.isChecked()
        t1, t2 = (line.value() for line in self.cursors)
        for s in self.series.values():
            s.item.setText(COL_VALUE, _fmt(s.trace.last(), s.unit))
            if on:
                a, b = s.trace.at(t1), s.trace.at(t2)
                s.item.setText(COL_C1, _fmt(a))
                s.item.setText(COL_C2, _fmt(b))
                s.item.setText(COL_DELTA, _fmt(b - a) if a is not None and b is not None else "")
        if on:
            dt = t2 - t1
            freq = f" ({1 / abs(dt):.4g} Hz)" if dt else ""
            stamp = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"  # noqa: E731
            self.readout.setText(f"C1 {stamp(t1)}   C2 {stamp(t2)}   Δt {dt * 1000:.1f} ms{freq}")
        else:
            self.readout.setText("")

    # --- the signal list ---------------------------------------------------------------------

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        s = self.series.get(item.data(COL_NAME, Qt.UserRole))
        if s is None or column != COL_NAME:
            return
        visible = item.checkState(COL_NAME) == Qt.Checked
        if visible != s.visible:
            s.visible = visible
            s.curve.setVisible(visible)
            self._dirty = True
            self.changed.emit()

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            path = item.data(COL_NAME, Qt.UserRole)
            s = self.series[path]
            axes = menu.addMenu("Y axis")
            for k in range(1, MAX_AXES + 1):
                a = QAction(f"Axis {k}" + (" (left)" if k == 1 else " (right)"), axes, checkable=True,
                            checked=s.axis == k)
                a.triggered.connect(lambda _=False, k=k: self.set_axis(path, k))
                axes.addAction(a)
            menu.addAction("Colour...", lambda: self._pick_color(path))
            menu.addAction("Remove from chart", lambda: self.remove(path))
            menu.addSeparator()
        if self.series:
            menu.addAction("Remove all", lambda: [self.remove(p) for p in list(self.series)])
        if not menu.isEmpty():
            menu.exec(self.list.viewport().mapToGlobal(pos))

    def _pick_color(self, path: str) -> None:
        c = QColorDialog.getColor(QColor(self.series[path].color), self, f"Colour of {path}")
        if c.isValid():
            self.set_color(path, c.name())

