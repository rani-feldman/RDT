"""The Target panel (GUI-14): one card per MCU, with its tag, whether it answers and how fast,
whether its ELF matches the firmware, and, opened, its variables and memory ranges. It replaces
the Variables panel and the toolbar's image box."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from ..memmap import MemoryMap, load_elf_sections
from ..profile import Profile
from ..session import LinkState
from ..target import McuConfig, Target, mcu_color, mcu_short
from .variables import SymbolBrowser

OK, AMBER, RED, GREY = "#2e9d4a", "#d99a0b", "#d23b3b", "#8a8f98"
RANK = {GREY: 0, OK: 1, AMBER: 2, RED: 3}


def link_health(t: Target | None, cfg: McuConfig, state) -> tuple[str, str]:
    """(colour, text) for whether an MCU answers: its reply time, timeouts in the last minute,
    silent, or no link."""
    if t is None or state is None or state == LinkState.CLOSED:
        return GREY, "not connected"
    if state != LinkState.UP:
        return RED, "link down"
    st = t.session.mcu_status(cfg.id)
    if st.silent:
        return GREY, "silent: not answering"
    misses = st.recent_timeouts()
    ms = "" if st.reply_ms is None else f"{st.reply_ms:.0f} ms"
    timeout_ms = t.session.timeout_s(cfg.id) * 1000.0
    if misses or (st.reply_ms is not None and st.reply_ms > timeout_ms / 2):
        miss = f"{misses} timeout{'s' if misses != 1 else ''}" if misses else ""
        return AMBER, " · ".join(p for p in (ms, miss) if p)
    return OK, f"✓ {ms}" if ms else "✓ answering"


def elf_health(t: Target | None, cfg: McuConfig, up: bool) -> tuple[str, str]:
    """(colour, text) for the MCU's ELF and whether it matches the firmware (SYM-8)."""
    name = cfg.elf.name if cfg.elf else ""
    if not cfg.elf:
        return GREY, "no ELF"
    if t is None or not up:
        return GREY, name
    ident = t.mcu(cfg.name).identity
    if ident is None:
        return GREY, f"? {name}: not checked yet"
    if ident.status == "match":
        return OK, f"✓ {name}"
    if ident.status == "mismatch":
        return RED, f"✗ {name} does not match"
    if ident.status == "unread":
        return GREY, f"? {name}: not read"
    return GREY, f"{name}: not checked"


class McuCard(QFrame):
    """One MCU. Click its title to open it and show its variables."""

    toggled = Signal(str)

    def __init__(self, main, cfg: McuConfig):
        super().__init__()
        self.main, self.cfg = main, cfg
        self.setObjectName("card")
        self.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(3)
        head = QHBoxLayout()
        self.tag = QLabel(mcu_short(cfg))
        self.name = QLabel(f"<b>{cfg.name}</b>")
        self.id = QLabel(f"id {cfg.id}")
        self.dot = QLabel("●")
        for w in (self.tag, self.name, self.id):
            head.addWidget(w)
        head.addStretch(1)
        head.addWidget(self.dot)
        layout.addLayout(head)
        self.link = QLabel()
        self.elf = QLabel()
        for w in (self.link, self.elf):
            w.setTextFormat(Qt.RichText)
            layout.addWidget(w)
        links = QHBoxLayout()
        self.vars_button = self._link_button("Variables")
        self.ranges_button = self._link_button("Memory ranges")
        self.vars_button.clicked.connect(lambda: self.toggled.emit(self.cfg.name))
        self.ranges_button.clicked.connect(self._toggle_ranges)
        links.addWidget(self.vars_button)
        links.addWidget(self.ranges_button)
        links.addStretch(1)
        layout.addLayout(links)
        self.browser = SymbolBrowser(main, mcu=cfg.name)
        self.browser.list.setMinimumHeight(90)
        self.browser.setVisible(False)
        layout.addWidget(self.browser, 1)
        self.ranges = QTreeWidget()
        self.ranges.setHeaderLabels(["Range", "Start", "Size", "Kind"])
        self.ranges.setRootIsDecorated(False)
        self.ranges.setVisible(False)
        self.ranges.setMaximumHeight(160)
        layout.addWidget(self.ranges)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._look = None
        self.restyle()

    @staticmethod
    def _link_button(text: str) -> QToolButton:
        b = QToolButton()
        b.setText(text + " ▸")
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def set_open(self, on: bool) -> None:
        self.browser.setVisible(on)
        self.vars_button.setText("Variables " + ("▾" if on else "▸"))

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and ev.position().y() < self.link.y():
            self.toggled.emit(self.cfg.name)   # a click on the title opens or closes the card
        super().mousePressEvent(ev)

    def restyle(self) -> None:
        """Colours from the MCU's tag colour and the theme."""
        color = mcu_color(self.cfg)
        self.tag.setText(mcu_short(self.cfg))
        self.tag.setStyleSheet(f"background: {color}; color: white; border-radius: 7px; padding: 1px 7px;"
                               " font-weight: bold")
        line = self.palette().mid().color().name()
        base = self.palette().base().color().name()
        self.setStyleSheet(f"#card {{ background: {base}; border: 1px solid {line}; border-left: 5px solid {color};"
                           " border-radius: 5px }")
        dim = self.palette().placeholderText().color().name()
        self.id.setStyleSheet(f"color: {dim}")

    def show_health(self, t: Target | None, state) -> None:
        up = state == LinkState.UP
        link = link_health(t, self.cfg, state)
        elf = elf_health(t, self.cfg, up)
        dim = self.palette().placeholderText().color().name()
        text = self.palette().text().color().name()
        look = (link, elf)
        if look == self._look:
            return
        self._look = look
        for label, word, (color, value) in ((self.link, "Link", link), (self.elf, "ELF", elf)):
            shown = text if color == OK and word == "Link" else dim if color == GREY else color
            label.setText(f"<span style='color:{dim}'>{word}</span>&nbsp;&nbsp;"
                          f"<span style='color:{shown}'>{value}</span>")
        worst = max((link[0], elf[0]), key=RANK.get)
        self.dot.setStyleSheet(f"color: {worst}")
        self.dot.setToolTip({OK: "Answering, ELF matches", AMBER: "Slow or timing out", RED: "ELF mismatch or link down",
                             GREY: "Not connected, silent or not checked"}[worst])

    def _toggle_ranges(self) -> None:
        on = self.ranges.isHidden()
        if on:
            self.fill_ranges()
        self.ranges.setVisible(on)
        self.ranges_button.setText("Memory ranges " + ("▾" if on else "▸"))

    def fill_ranges(self) -> None:
        """The memory RDT may read on this MCU: the ELF's sections and the workspace's ranges."""
        self.ranges.clear()
        t = self.main.device.target
        try:
            if t is not None:
                areas = t.mcu(self.cfg.name).memory.areas
            else:
                profile: Profile = self.main.ws.config.profile
                shared = [r for r in profile.ranges if r.mcu in (None, "", self.cfg.name, self.cfg.id)]
                sections = load_elf_sections(self.cfg.elf).sections if self.cfg.elf else []
                areas = MemoryMap(sections, shared + self.cfg.ranges).areas
        except Exception as e:   # an ELF that can't be read: say so in the list
            QTreeWidgetItem(self.ranges, [str(e), "", "", ""])
            return
        for a in areas:
            item = QTreeWidgetItem([a.name, f"0x{a.start:08X}", _size(a.size), a.kind])
            if not a.writable:
                item.setToolTip(0, "read-only")
            self.ranges.addTopLevelItem(item)
        for c in range(3):
            self.ranges.resizeColumnToContents(c)

    def _menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Check ELF again", lambda: self.main.check_identity([self.cfg.name]))
        menu.addAction("MCUs and ELF files...", self.main.edit_mcus)
        menu.addAction("Open in the memory view", lambda: self.main.show_memory(self.cfg.name))
        menu.exec(self.mapToGlobal(pos))


def _size(n: int) -> str:
    for unit, k in (("M", 1 << 20), ("K", 1 << 10)):
        if n >= k and n % k == 0:
            return f"{n // k}{unit}"
    return str(n)


class McuCards(QWidget):
    """The Target panel: the link, then one card per MCU; one card is open at a time."""

    add_requested = Signal(str)

    def __init__(self, main):
        super().__init__()
        self.main = main
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.title = QLabel()
        outer.addWidget(self.title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self.column = QVBoxLayout(holder)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(8)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        self.cards: dict[str, McuCard] = {}

    def set_workspace(self, ws) -> None:
        open_name = next((n for n, c in self.cards.items() if not c.browser.isHidden()), None)
        while self.column.count():
            w = self.column.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.cards = {}
        for cfg in ws.config.mcus:
            card = McuCard(self.main, cfg)
            card.toggled.connect(self.toggle)
            card.browser.add_requested.connect(self.add_requested.emit)
            card.browser.set_workspace(ws)
            self.cards[cfg.name] = card
            self.column.addWidget(card)
        self.column.addStretch(1)   # takes the room while every card is closed
        if self.cards:
            self.toggle(open_name if open_name in self.cards else next(iter(self.cards)), only_open=True)
        self.show_health(None, None)

    def toggle(self, name: str, only_open: bool = False) -> None:
        """Open one card (closing the others), or close it when it is open."""
        card = self.cards[name]
        on = only_open or card.browser.isHidden()
        for n, c in self.cards.items():
            c.set_open(on and n == name)
            self.column.setStretchFactor(c, 1 if on and n == name else 0)
        self.column.setStretch(self.column.count() - 1, 0 if on else 1)

    def browser(self, name: str) -> SymbolBrowser:
        return self.cards[name].browser

    def reload(self) -> None:
        for c in self.cards.values():
            c.browser.reload()

    def restyle(self) -> None:
        for c in self.cards.values():
            c.restyle()
            c._look = None

    def show_health(self, t: Target | None, state) -> None:
        conn = self.main.ws.config.connection
        where = conn.address + (f" · {conn.baud}" if conn.transport == "serial" else f" · {conn.transport}")
        name = Path(self.main.ws.path).stem if self.main.ws.path else "Target"
        self.title.setText(f"<b>{name}</b> · {where}")
        for c in self.cards.values():
            c.show_health(t, state)
