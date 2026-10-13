"""Variable view: the watch tabs and their trees, the symbol browser, and the watch item settings dialog."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..errors import ConfigError, LinkDown, McuSilent, RdtError
from ..scaling import FORMATS, Display
from ..stats import StatsBook, numeric
from ..symbols import Location, load_symbols
from ..workspace import Reading, Shown, Tab, Watch, Workspace
from .device import describe_error

COL_NAME, COL_VALUE, COL_TYPE, COL_ADDR, COL_RATE, COL_MIN, COL_MAX, COL_MEAN, COL_COUNT = range(9)
STATS_COLUMNS = (COL_MIN, COL_MAX, COL_MEAN, COL_COUNT)
POLL_KEY = "poll"
GREY = QBrush(QColor("#888888"))
READ_ONLY_CHOICES = ("Workspace default", "Read-only", "Writable")
WARN = QBrush(QColor("#c87800"))
ERROR = QBrush(QColor("#c03030"))
PATH_ROLE = Qt.UserRole
CELL_STEP = 100


@dataclass
class _ItemState:
    """What the tree knows about one watched item between reads."""

    watch: Watch
    item: QTreeWidgetItem
    nodes: dict[str, QTreeWidgetItem] = field(default_factory=dict)
    shown: dict[str, Shown] = field(default_factory=dict)
    loc: Location | None = None
    aggregate: bool | None = None   # unknown until the first read
    max_cells: int = CELL_STEP
    due: float = 0.0
    last_done: float | None = None
    period: float | None = None     # achieved time between reads, smoothed
    note: str | None = None         # after an ELF rebuild: moved, retyped or gone
    gone: bool = False
    force: bool = True              # read at the next tick whatever the interval


class WatchTree(QTreeWidget):
    """Debugger-style watch tree for one watch tab. Top-level rows are the tab's items; structs and
    arrays open to every field and cell. A collapsed struct or array is not read. The trees don't
    poll on their own: the tabs widget reads what every tree has due in one poll."""

    changed = Signal()            # the watch list or an item's settings changed
    chart_requested = Signal(str, str)   # path, unit: the user asked to chart this value

    def __init__(self, main, tab: Tab):
        super().__init__()
        self.main = main
        self.tab = tab
        self.setColumnCount(9)
        self.setHeaderLabels(["Name", "Value", "Type", "Address", "Rate", "Min", "Max", "Mean", "Samples"])
        for c in STATS_COLUMNS:
            self.setColumnHidden(c, True)
            self.setColumnWidth(c, 80)
        self.header().setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.setColumnWidth(COL_NAME, 220)
        self.setColumnWidth(COL_VALUE, 180)
        self.setColumnWidth(COL_TYPE, 130)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.itemChanged.connect(self._item_changed)
        self.itemExpanded.connect(self._expanded)
        self.states: dict[str, _ItemState] = {}
        self.stats = StatsBook()
        self._editing: tuple[str, str] | None = None   # (expr of the item, path) being edited

    @property
    def ws(self) -> Workspace:
        return self.main.ws

    # --- the watch list ----------------------------------------------------------------------

    def rebuild(self) -> None:
        self.blockSignals(True)
        self.clear()
        self.states.clear()
        for w in self.tab.watches:
            self._add_row(w)
        self.blockSignals(False)

    def _add_row(self, w: Watch) -> _ItemState:
        item = QTreeWidgetItem([w.expr] + [""] * 8)
        item.setData(COL_NAME, PATH_ROLE, w.expr)
        self.addTopLevelItem(item)
        st = _ItemState(w, item, nodes={w.expr: item})
        self.states[w.expr] = st
        self._decorate(st)
        return st

    def add_watch(self, expr: str) -> Watch | None:
        try:
            w = self.ws.add(Watch(expr), self.tab.name)
        except ConfigError as e:
            self.main.status(str(e))
            return None
        self._add_row(w)
        self.changed.emit()
        return w

    def remove_watch(self, expr: str) -> None:
        st = self.states.pop(expr, None)
        if st is None:
            return
        self.ws.remove(expr, self.tab.name)
        self.stats.reset(expr)
        self.takeTopLevelItem(self.indexOfTopLevelItem(st.item))
        self.changed.emit()

    def replace_watch(self, old: Watch, new: Watch) -> None:
        """New settings for an item, in this tab and every other tab that has it."""
        self.ws.replace(new)
        self.main.watch_tabs.replaced(old, new)
        self.changed.emit()

    def replaced(self, old: Watch, new: Watch) -> None:
        st = self.states.get(new.expr)
        if st is None:
            return
        st.watch, st.force = new, True
        if new.display != old.display:
            self.reset_stats(new.expr)   # the old numbers were in other units
        self._decorate(st)

    def _decorate(self, st: _ItemState) -> None:
        w = st.watch
        tips = []
        if self.ws.read_only_reason(w.expr):
            tips.append("read-only")
        if w.min is not None or w.max is not None:
            tips.append(f"limits {'' if w.min is None else f'{w.min:g}'}..{'' if w.max is None else f'{w.max:g}'}")
        if w.confirm:
            tips.append("asks before writing")
        poll = w.interval_ms(self.ws.poll_ms)
        tips.append("polled by hand" if poll is None else f"every {poll} ms")
        if st.note:
            tips.append(st.note)
        st.item.setToolTip(COL_NAME, "; ".join(tips))

    # --- polling -----------------------------------------------------------------------------

    def due(self, now: float, polling: bool = True) -> list[_ItemState]:
        """The items that should be read now. With ``polling`` off, only those read by hand."""
        out = []
        for expr, st in self.states.items():
            if st.gone:
                continue
            interval = st.watch.interval_ms(self.ws.poll_ms)
            if not st.force and (not polling or interval is None or now < st.due):
                continue
            if st.aggregate and not st.item.isExpanded() and not st.force and not self.main.reads_closed(expr):
                continue   # a collapsed struct or array is not read
            out.append(st)
        return out

    def polled(self, expr: str, now: float) -> None:
        """A read of this item was started: keep its cadence; one that fell behind starts again from now."""
        st = self.states.get(expr)
        if st is None:
            return
        st.force = False
        step = (st.watch.interval_ms(self.ws.poll_ms) or 0) / 1000.0
        st.due = st.due + step if now - st.due < step else now + step

    def tick(self) -> None:
        self.main.watch_tabs.tick()

    def read_now(self, expr: str) -> None:
        st = self.states.get(expr)
        if st is not None:
            st.force = True
            self.tick()

    def deliver(self, r: Reading) -> None:
        st = self.states.get(r.watch.expr)
        if st is None:
            return   # removed while it was being read
        if r.error is not None:
            self._failed(st, r.error)
        else:
            self._done(st, (r.loc, r.shown))

    def _done(self, st: _ItemState, result) -> None:
        loc, rows = result
        st.loc = loc
        now = time.monotonic()
        if st.last_done is not None:
            dt = now - st.last_done
            st.period = dt if st.period is None else 0.7 * st.period + 0.3 * dt
        st.last_done = now
        self.blockSignals(True)
        try:
            self._fill(st, rows)
        finally:
            self.blockSignals(False)

    def _fill(self, st: _ItemState, rows: list[Shown]) -> None:
        st.aggregate = len(rows) > 1 or (bool(rows) and rows[0].text is None)
        stack: list[QTreeWidgetItem] = []
        seen = set()
        for r in rows:
            if r.depth == 0:
                item = st.item
            else:
                parent = stack[r.depth - 1]
                item = st.nodes.get(r.path)
                if item is None or item.parent() is not parent:
                    item = QTreeWidgetItem([self._label(r.path, st.watch.expr)] + [""] * 8)
                    item.setData(COL_NAME, PATH_ROLE, r.path)
                    parent.addChild(item)
                    st.nodes[r.path] = item
            del stack[r.depth:]
            stack.append(item)
            seen.add(r.path)
            st.shown[r.path] = r
            if r.text is None:
                n = item.childCount()
                text = "{...}" if "struct" in r.type_name or "union" in r.type_name or not r.type_name.endswith("]") \
                    else f"[{r.type_name.rsplit('[', 1)[-1]}"
                item.setText(COL_VALUE, text if not item.isExpanded() or not n else "")
            else:
                item.setText(COL_VALUE, r.text)
                self._show_stats(item, self.stats.add(r.path, r.value))
            item.setText(COL_TYPE, r.type_name)
            item.setText(COL_ADDR, f"0x{r.addr:08X}" if r.raw is not None or r.text is None else "")
            brush = GREY if (self._read_only(r.path) or not r.scalar) and r.text is not None else QBrush()
            item.setForeground(COL_VALUE, WARN if st.note else brush)
        for path in [p for p in st.nodes if p not in seen]:
            node = st.nodes.pop(path)
            st.shown.pop(path, None)
            if node is not st.item and node.parent() is not None:
                node.parent().removeChild(node)
        rate = f"{1 / st.period:.1f}/s" if st.period and st.watch.interval_ms(self.ws.poll_ms) else ""
        st.item.setText(COL_RATE, rate)

    @staticmethod
    def _show_stats(item: QTreeWidgetItem, st) -> None:
        if st is None:
            for c in STATS_COLUMNS:
                item.setText(c, "")
            return
        for c, v in ((COL_MIN, st.min), (COL_MAX, st.max), (COL_MEAN, st.mean)):
            item.setText(c, f"{v:.6g}")
        item.setText(COL_COUNT, str(st.n))

    def reset_stats(self, expr: str | None = None) -> None:
        """Start the statistics again, for one watched item or for all."""
        self.stats.reset(expr)
        for e, st in self.states.items():
            if expr is None or e == expr:
                for node in st.nodes.values():
                    self._show_stats(node, None)

    def show_stats(self, on: bool) -> None:
        for c in STATS_COLUMNS:
            self.setColumnHidden(c, not on)

    def chartable(self, path: str) -> tuple[_ItemState, Shown] | None:
        """The item and value behind a path, if it can be charted: a number, a bool or an enum."""
        for st in self.states.values():
            row = st.shown.get(path)
            if row is not None and row.text is not None and numeric(row.value) is not None:
                return st, row
        return None

    def chart(self, path: str) -> None:
        found = self.chartable(path)
        if found is None:
            self.main.status(f"{path} is not a number, so it can't be charted")
            return
        st, _ = found
        self.chart_requested.emit(path, st.watch.display.unit if path == st.watch.expr else "")
        if st.aggregate:
            st.force = True   # read it even while collapsed

    def selected_paths(self) -> list[str]:
        return [p for i in self.selectedItems() if (p := i.data(COL_NAME, PATH_ROLE))]

    @staticmethod
    def _label(path: str, top: str) -> str:
        """A child's own part of its path: ``.speed``, ``[3]``."""
        if path.startswith(top):
            rest = path[len(top):]
            if rest.startswith("."):
                return rest[1:]
            return rest
        _, _, rest = path.rpartition(".")
        return rest

    def _failed(self, st: _ItemState, e: Exception) -> None:
        if self.states.get(st.watch.expr) is not st:
            return
        self.blockSignals(True)
        text = "link down" if isinstance(e, LinkDown) else ("MCU silent" if isinstance(e, McuSilent)
                                                           else describe_error(e))
        for node in st.nodes.values():
            node.setForeground(COL_VALUE, GREY)
        st.item.setText(COL_VALUE, text)
        st.item.setForeground(COL_VALUE, GREY if isinstance(e, (LinkDown, McuSilent)) else ERROR)
        st.item.setToolTip(COL_VALUE, describe_error(e))
        self.blockSignals(False)

    def _expanded(self, item: QTreeWidgetItem) -> None:
        if item.parent() is None:
            self.read_now(item.data(COL_NAME, PATH_ROLE))

    # --- after an ELF rebuild ----------------------------------------------------------------

    def remapped(self, expr: str, new, note: str | None) -> None:
        st = self.states.get(expr)
        if st is None:
            return
        st.note = note
        st.gone = new is None
        st.force = True
        if st.gone:
            st.item.setText(COL_VALUE, "gone")
            st.item.setForeground(COL_VALUE, GREY)
        self._decorate(st)

    # --- writing -----------------------------------------------------------------------------

    def _read_only(self, path: str) -> bool:
        return self.ws.read_only_reason(path) is not None

    def _owner(self, item: QTreeWidgetItem) -> _ItemState | None:
        top = item
        while top.parent() is not None:
            top = top.parent()
        return self.states.get(top.data(COL_NAME, PATH_ROLE))

    def _double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        st = self._owner(item)
        path = item.data(COL_NAME, PATH_ROLE)
        if st is None or path is None or column != COL_VALUE:
            return
        row = st.shown.get(path)
        if row is None or not row.scalar:
            return
        reason = self.ws.read_only_reason(path)
        if reason:
            self.main.status(f"{path} is {reason}")
            return
        self._editing = (st.watch.expr, path)
        self.blockSignals(True)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setText(COL_VALUE, self._edit_text(st, path, row))
        self.blockSignals(False)
        self.editItem(item, COL_VALUE)

    def _edit_text(self, st: _ItemState, path: str, row: Shown) -> str:
        unit = st.watch.display.unit if path == st.watch.expr else ""
        text = row.text or ""
        if unit and text.endswith(" " + unit):
            text = text[: -len(unit) - 1]
        if " (" in text and path != st.watch.expr or (" (" in text and not st.watch.display.scaled):
            head = text.split(" (", 1)[0]
            name = getattr(row.value, "name", None)
            text = name or head
        return text

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._editing is None or column != COL_VALUE:
            return
        expr, path = self._editing
        if item.data(COL_NAME, PATH_ROLE) != path:
            return
        self._editing = None
        self.blockSignals(True)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.blockSignals(False)
        self.write(path, item.text(COL_VALUE).strip(), expr)

    def write(self, path: str, text: str, owner: str | None = None) -> None:
        """Write ``text`` (in shown units) to ``path`` through the workspace's write safety."""
        exact, parent = self.ws.guard_for(path)
        reason = self.ws.read_only_reason(path)
        if reason:
            self.main.status(f"{path} is {reason}")
            return
        if (exact and exact.confirm) or (parent and parent.confirm):
            unit = exact.display.unit if exact else ""
            answer = QMessageBox.question(self, "Write value", f"Write {text}{' ' + unit if unit else ''} to {path}?")
            if answer != QMessageBox.Yes:
                self.main.status(f"{path}: write cancelled")
                self.read_now(owner or path)
                return
        ws = self.ws

        def done(_):
            self.main.status(f"{path} = {text} (confirmed by read-back)")
            self.read_now(owner or path)

        def failed(e):
            self.main.status(f"write {path}: {describe_error(e)}", error=True)
            self.read_now(owner or path)

        self.main.device.submit(lambda t: ws.write(t, path, text, confirm=lambda q: True), done, failed)

    # --- menus -------------------------------------------------------------------------------

    def _menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        st = self._owner(item)
        path = item.data(COL_NAME, PATH_ROLE)
        if st is None:
            return
        menu = QMenu(self)
        top = item.parent() is None
        menu.addAction("Read now", lambda: self.read_now(st.watch.expr))
        row = st.shown.get(path)
        if not top and path:
            menu.addAction("Watch this on its own", lambda: self.add_watch(path))
        if path and self.chartable(path):
            if self.main.chart.has(path):
                menu.addAction("Remove from chart", lambda: self.main.chart.remove(path))
            else:
                menu.addAction("Add to chart", lambda: self.chart(path))
        if row is not None and row.scalar and not self._read_only(path):
            menu.addAction("Write...", lambda: self._double_clicked(item, COL_VALUE))
        if row is not None and row.type_name.endswith("*") and row.raw:
            menu.addAction("Watch what it points to", lambda: self.add_watch(_deref(path)))
        if item.text(COL_VALUE).startswith("... "):
            menu.addAction(f"Show {CELL_STEP} more cells", lambda: self._more_cells(st))
        if top:
            menu.addSeparator()
            fmt = menu.addMenu("Format")
            for f in FORMATS:
                a = QAction(f, fmt, checkable=True, checked=st.watch.display.format == f)
                a.triggered.connect(lambda _=False, f=f: self.set_format(st.watch.expr, f))
                fmt.addAction(a)
            menu.addAction("Settings...", lambda: self.edit_settings(st.watch.expr))
            menu.addAction("Reset statistics", lambda: self.reset_stats(st.watch.expr))
            others = [t for t in self.ws.tabs if t is not self.tab and not t.watch(st.watch.expr)]
            if others:
                copy = menu.addMenu("Also show in tab")
                for t in others:
                    copy.addAction(t.name, lambda t=t: self.main.watch_tabs.tree(t).add_watch(st.watch.expr))
            menu.addAction("Remove from this tab", lambda: self.remove_watch(st.watch.expr))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _more_cells(self, st: _ItemState) -> None:
        st.max_cells += CELL_STEP
        self.read_now(st.watch.expr)

    def set_format(self, expr: str, fmt: str) -> None:
        st = self.states[expr]
        d = st.watch.display
        new = Watch.from_dict({**st.watch.to_dict(), "format": fmt}) if fmt != d.format else st.watch
        if new is not st.watch:
            self.replace_watch(st.watch, new)
            self.read_now(new.expr)

    def edit_settings(self, expr: str) -> None:
        st = self.states[expr]
        dlg = WatchDialog(st.watch, self)
        if dlg.exec() == QDialog.Accepted:
            try:
                new = dlg.result_watch()
            except (ConfigError, ValueError) as e:
                QMessageBox.warning(self, "Watch settings", str(e))
                return
            self.replace_watch(st.watch, new)
            self.read_now(new.expr)

    def selected_exprs(self) -> list[str]:
        return [i.data(COL_NAME, PATH_ROLE) for i in self.selectedItems() if i.parent() is None]


class WatchTabs(QTabWidget):
    """The watch tabs: one watch tree per tab of the workspace. Every tab keeps polling, not only
    the one in front, and one poll reads what all of them have due: an item in several tabs is
    read once and shown in each."""

    changed = Signal()                   # tabs, their items or an item's settings changed
    sampled = Signal(object)             # list[Reading]: everything one poll read, for the chart and the log
    chart_requested = Signal(str, str)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.trees: list[WatchTree] = []
        self._stats_shown = False
        self.polling = True   # off: nothing is read until Poll now (F6), whatever the intervals
        self.setMovable(True)
        self.setDocumentMode(True)
        self.tabBar().tabMoved.connect(self._moved)
        self.tabBar().tabBarDoubleClicked.connect(lambda i: self.rename_tab(i) if i >= 0 else self.new_tab())
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._menu)
        plus = QToolButton()
        plus.setText("+")
        plus.setToolTip("New watch tab (Ctrl+Shift+T)")
        plus.setAutoRaise(True)
        plus.clicked.connect(lambda: self.new_tab())
        self.setCornerWidget(plus, Qt.TopRightCorner)

    @property
    def ws(self) -> Workspace:
        return self.main.ws

    # --- tabs --------------------------------------------------------------------------------

    def rebuild(self) -> None:
        self.blockSignals(True)
        while self.count():
            w = self.widget(0)
            self.removeTab(0)
            w.deleteLater()
        self.trees = []
        self.ws.ensure_tab()
        for tab in self.ws.tabs:
            self._add_tree(tab)
        self.setCurrentIndex(0)
        self.blockSignals(False)

    def _add_tree(self, tab: Tab, index: int | None = None) -> WatchTree:
        tree = WatchTree(self.main, tab)
        tree.changed.connect(self.changed)
        tree.chart_requested.connect(self.chart_requested)
        tree.show_stats(self._stats_shown)
        tree.rebuild()
        index = len(self.trees) if index is None else index
        self.trees.insert(index, tree)
        self.insertTab(index, tree, tab.name)
        return tree

    def current(self) -> WatchTree:
        return self.currentWidget()

    def tree(self, tab: Tab) -> WatchTree:
        return next(t for t in self.trees if t.tab is tab)

    def new_tab(self, name: str | None = None) -> WatchTree | None:
        if name is None:
            n = len(self.ws.tabs) + 1
            while self.ws.has_tab(f"Watch {n}"):
                n += 1
            name, ok = QInputDialog.getText(self, "New watch tab", "Name:", text=f"Watch {n}")
            if not ok:
                return None
        try:
            tab = self.ws.add_tab(name)
        except ConfigError as e:
            self.main.status(str(e), error=True)
            return None
        tree = self._add_tree(tab)
        self.setCurrentWidget(tree)
        self.changed.emit()
        return tree

    def rename_tab(self, index: int | None = None, name: str | None = None) -> None:
        index = self.currentIndex() if index is None else index
        tree = self.trees[index]
        if name is None:
            name, ok = QInputDialog.getText(self, "Rename watch tab", "Name:", text=tree.tab.name)
            if not ok:
                return
        try:
            self.ws.rename_tab(tree.tab.name, name)
        except ConfigError as e:
            self.main.status(str(e), error=True)
            return
        self.setTabText(index, tree.tab.name)
        self.changed.emit()

    def close_tab(self, index: int | None = None, ask: bool = True) -> None:
        index = self.currentIndex() if index is None else index
        tree = self.trees[index]
        if len(self.trees) == 1:
            self.main.status("The last watch tab can't be removed; rename it or remove its items instead")
            return
        n = len(tree.tab.watches)
        if ask and n and QMessageBox.question(
                self, "Remove watch tab", f"Remove tab {tree.tab.name!r} and its {n} items?") != QMessageBox.Yes:
            return
        self.ws.remove_tab(tree.tab.name)
        self.trees.remove(tree)
        self.removeTab(index)
        tree.deleteLater()
        self.changed.emit()

    def _moved(self, frm: int, to: int) -> None:
        tree = self.trees.pop(frm)
        self.trees.insert(to, tree)
        self.ws.tabs[:] = [t.tab for t in self.trees]
        self.changed.emit()

    def _menu(self, pos) -> None:
        index = self.tabBar().tabAt(pos)
        menu = QMenu(self)
        menu.addAction("New tab...", lambda: self.new_tab())
        if index >= 0:
            menu.addAction("Rename...", lambda: self.rename_tab(index))
            a = menu.addAction("Remove", lambda: self.close_tab(index))
            a.setEnabled(len(self.trees) > 1)
        menu.exec(self.tabBar().mapToGlobal(pos))

    # --- across every tab --------------------------------------------------------------------

    def states(self):
        """Every (tree, item state) pair, in tab order."""
        for tree in self.trees:
            for st in tree.states.values():
                yield tree, st

    def exprs(self) -> list[str]:
        """Every watched expression once."""
        return list(dict.fromkeys(st.watch.expr for _, st in self.states()))

    def add_watch(self, expr: str) -> Watch | None:
        return self.current().add_watch(expr)

    def replaced(self, old: Watch, new: Watch) -> None:
        for tree in self.trees:
            tree.replaced(old, new)

    def force(self, expr: str | None = None) -> None:
        """Read an item (or everything) at the next tick."""
        for _, st in self.states():
            if expr is None or st.watch.expr == expr:
                st.force = True

    def read_all(self) -> None:
        self.force()
        self.tick()

    def decorate(self) -> None:
        for tree, st in self.states():
            tree._decorate(st)

    def show_stats(self, on: bool) -> None:
        self._stats_shown = on
        for tree in self.trees:
            tree.show_stats(on)

    def reset_stats(self) -> None:
        for tree in self.trees:
            tree.reset_stats()

    def tick(self, now: float | None = None) -> None:
        """Read every item that is due in any tab, all in one poll. Called by the main window's
        poll timer. While a poll is still running nothing new starts, so a slow link falls behind
        instead of queueing reads."""
        device = self.main.device
        if device.target is None or device.busy(POLL_KEY):
            return
        now = time.monotonic() if now is None else now
        due: dict[str, Watch] = {}
        cells: dict[str, int] = {}
        for tree in self.trees:
            for st in tree.due(now, self.polling):
                due.setdefault(st.watch.expr, st.watch)
                cells[st.watch.expr] = max(cells.get(st.watch.expr, 0), st.max_cells)
        if not due:
            return
        ws, watches = self.ws, list(due.values())
        job = device.submit(lambda t: ws.read_many(t, watches, cells, headers=True), self._polled,
                            self._poll_failed, key=POLL_KEY)
        if job is not None:
            for expr in due:
                for tree in self.trees:
                    tree.polled(expr, now)

    def _polled(self, readings: list[Reading]) -> None:
        for r in readings:
            for tree in self.trees:
                tree.deliver(r)
        self.sampled.emit(readings)

    def _poll_failed(self, e: Exception) -> None:
        self.main.status(f"poll: {describe_error(e)}", error=True)

    def remap(self, mcus: list[str]) -> None:
        """Find the items of the reloaded MCUs again by name, and flag what changed."""
        items = {}
        for _, st in self.states():
            if st.loc is not None and st.loc.mcu in mcus:
                items.setdefault(st.watch.expr, st.loc)
        if not items:
            return

        def job(t):
            return [(expr, *t.remap(loc)) for expr, loc in items.items()]

        self.main.device.submit(job, self._remapped, lambda e: self.main.status(describe_error(e)))

    def _remapped(self, results) -> None:
        for expr, new, note in results:
            for tree in self.trees:
                tree.remapped(expr, new, note)
            if note:
                self.main.status(note)


def _deref(path: str) -> str:
    mcu, _, expr = path.partition(":")
    return f"{mcu}:*{expr}" if not expr.startswith("*") else f"{mcu}:*({expr})"


class WatchDialog(QDialog):
    """Settings of one watched item: format, scaling, polling and write safety."""

    def __init__(self, w: Watch, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Settings: {w.expr}")
        self.expr = w.expr
        d = w.display
        form = QFormLayout(self)
        self.format = QComboBox()
        self.format.addItems(FORMATS)
        self.format.setCurrentText(d.format)
        self.q = QLineEdit(d.q or "")
        self.q.setPlaceholderText("none, or Q15, Q31, Q3.12")
        self.scale = QLineEdit(f"{d.k:g}")
        self.offset = QLineEdit(f"{d.offset:g}")
        self.unit = QLineEdit(d.unit)
        poll_row = QHBoxLayout()
        self.poll = QComboBox()
        self.poll.addItems(["global", "manual", "ms"])
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(1, 3_600_000)
        self.poll_ms.setSuffix(" ms")
        if isinstance(w.poll, int):
            self.poll.setCurrentText("ms")
            self.poll_ms.setValue(w.poll)
        else:
            self.poll.setCurrentText(w.poll)
            self.poll_ms.setValue(500)
        self.poll_ms.setEnabled(isinstance(w.poll, int))
        self.poll.currentTextChanged.connect(lambda t: self.poll_ms.setEnabled(t == "ms"))
        poll_row.addWidget(self.poll)
        poll_row.addWidget(self.poll_ms)
        self.read_only = QComboBox()
        self.read_only.addItems(READ_ONLY_CHOICES)
        self.read_only.setCurrentIndex({None: 0, True: 1, False: 2}[w.read_only])
        self.read_only.setToolTip("Workspace default follows Watch > All items read-only")
        self.min = QLineEdit("" if w.min is None else f"{w.min:g}")
        self.max = QLineEdit("" if w.max is None else f"{w.max:g}")
        for e in (self.min, self.max):
            e.setPlaceholderText("no limit")
        self.confirm = QCheckBox("Ask before writing")
        self.confirm.setChecked(w.confirm)
        form.addRow("Format", self.format)
        form.addRow("Fixed point", self.q)
        form.addRow("Scale", self.scale)
        form.addRow("Offset", self.offset)
        form.addRow("Unit", self.unit)
        form.addRow(QLabel("Shown = raw / 2^q × scale + offset"))
        form.addRow("Polling", poll_row)
        form.addRow("Read-only", self.read_only)
        form.addRow("Minimum", self.min)
        form.addRow("Maximum", self.max)
        form.addRow("Confirm", self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self) -> None:
        try:
            self.result_watch()
        except (ConfigError, ValueError) as e:
            QMessageBox.warning(self, "Watch settings", str(e))
            return
        self.accept()

    def result_watch(self) -> Watch:
        poll = self.poll_ms.value() if self.poll.currentText() == "ms" else self.poll.currentText()
        display = Display(self.format.currentText(), self.q.text().strip() or None, float(self.scale.text() or 1),
                          float(self.offset.text() or 0), self.unit.text().strip())
        return Watch(self.expr, display, poll, (None, True, False)[self.read_only.currentIndex()],
                     float(self.min.text()) if self.min.text().strip() else None,
                     float(self.max.text()) if self.max.text().strip() else None, self.confirm.isChecked())


class SymbolBrowser(QWidget):
    """Browse or search an MCU's variables from its ELF and add them to the watch list."""

    add_requested = Signal(str)

    def __init__(self, main):
        super().__init__()
        self.main = main
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.mcu = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search, such as motor*")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.mcu)
        top.addWidget(self.search, 1)
        layout.addLayout(top)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Name", "Type", "Size", "Address"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setColumnWidth(0, 160)
        self.list.itemDoubleClicked.connect(lambda item, _: self.add_requested.emit(item.data(0, PATH_ROLE)))
        layout.addWidget(self.list, 1)
        self.add_button = QPushButton("Add to watch list")
        self.add_button.clicked.connect(self._add_selected)
        layout.addWidget(self.add_button)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.mcu.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self._tables: dict[str, object] = {}

    def set_workspace(self, ws: Workspace) -> None:
        self._tables.clear()
        self.mcu.blockSignals(True)
        self.mcu.clear()
        for m in ws.config.mcus:
            self.mcu.addItem(m.name, m.elf)
        self.mcu.blockSignals(False)
        self.refresh()

    def _symbols(self):
        name = self.mcu.currentText()
        if name not in self._tables:
            elf = self.mcu.currentData()
            if elf is None:
                self._tables[name] = None
            else:
                try:
                    self._tables[name] = load_symbols(elf)
                except RdtError as e:
                    self._tables[name] = e
        return self._tables[name]

    def reload(self) -> None:
        """Forget the loaded symbols, after an ELF rebuild."""
        self._tables.clear()
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        if not self.mcu.currentText():
            self.message.setText("No MCUs in the workspace.")
            return
        table = self._symbols()
        if table is None:
            self.message.setText(f"MCU {self.mcu.currentText()} has no ELF; watch raw addresses such as "
                                 f"@{self.mcu.currentText()}:0x20000000:u32.")
            return
        if isinstance(table, Exception):
            self.message.setText(str(table))
            return
        found = table.search(self.search.text().strip() or "*")
        mcu = self.mcu.currentText()
        shown = found[:2000]
        for v in shown:
            item = QTreeWidgetItem([v.qualified_name, v.type.name, str(v.size), f"0x{v.address:08X}"])
            item.setData(0, PATH_ROLE, f"{mcu}:{v.qualified_name}")
            self.list.addTopLevelItem(item)
        more = f" (first {len(shown)} shown)" if len(found) > len(shown) else ""
        self.message.setText(f"{len(found)} variables{more}. Double-click to watch.")

    def _add_selected(self) -> None:
        for item in self.list.selectedItems():
            self.add_requested.emit(item.data(0, PATH_ROLE))
