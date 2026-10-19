"""Record the full live walkthrough of RDT on a virtual X screen: a real mouse pointer (xdotool)
clicks through every part of the GUI against the simulator, then the CLI in a terminal, while ffmpeg
grabs the screen. Each step's narration is made first, so the step lasts as long as its sentence.
Every pointer position and focus point is logged, so assemble.py can zoom in on it.

DISPLAY=:99 python tools/media/walkthrough.py [--voice-only]
  -> WORK/live/screen.mkv, steps.json, focus.json, errors.json, say/*.wav
"""
import json, os, queue, shutil, subprocess, sys, tempfile, threading, time, traceback
from pathlib import Path

from common import HERE, ROOT, WORK, VOICE, kokoro, say as spoken

OUT = WORK / "live"
(OUT / "say").mkdir(parents=True, exist_ok=True)
TEMPLATE = HERE / "bench"
BENCH_DIR = WORK / "bench"
BENCH = BENCH_DIR / "sim_bench.rdtw"
FILES = BENCH_DIR / "files"
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["RDT_UI_FILE"] = str(Path(tempfile.mkdtemp()) / "ui.toml")
os.environ["RDT_HISTORY_FILE"] = str(Path(tempfile.mkdtemp()) / "console_history.txt")
os.environ["RDT_NO_UPDATE_CHECK"] = "1"
sys.path.insert(0, str(ROOT))

import soundfile as sf

from PySide6.QtCore import QTimer, QPoint
from PySide6.QtWidgets import QApplication, QAbstractButton, QTreeWidgetItemIterator

# --- the walkthrough: (chapter or None, narration, zoom, action) ------------------------------
STEPS = []


def step(text, chapter=None, zoom=1.0):
    def deco(fn):
        STEPS.append((chapter, text, zoom, fn))
        return fn
    return deco


# --- main-thread access from the driver thread ---------------------------------------------
JOBS = queue.Queue()


def ui(fn, tries=25):
    for _ in range(tries - 1):
        try:
            return _ui(fn)
        except (LookupError, KeyError, IndexError, AttributeError, RuntimeError):
            time.sleep(0.15)
    return _ui(fn)


def _ui(fn):
    box, done = {}, threading.Event()
    JOBS.put((fn, box, done))
    done.wait(10)
    if "err" in box:
        raise box["err"]
    return box.get("val")


def _run_jobs():
    while not JOBS.empty():
        fn, box, done = JOBS.get()
        try:
            box["val"] = fn()
        except Exception as e:      # reported in the driver thread
            box["err"] = e
        done.set()


def centre(widget, dx=0, dy=0):
    p = widget.mapToGlobal(widget.rect().center())
    return p.x() + dx, p.y() + dy


def strip(t):
    return t.replace("&", "").split("\t")[0].strip()


def menu_title(text):
    bar = W.menu_bar
    for a in bar.actions():
        if strip(a.text()) == text:
            p = bar.mapToGlobal(bar.actionGeometry(a).center())
            return p.x(), p.y()
    raise KeyError(text)


def popup_item(text):
    from PySide6.QtWidgets import QMenu
    active = QApplication.activePopupWidget()
    menus = ([active] if active else []) + [w for w in QApplication.topLevelWidgets()
                                            if isinstance(w, QMenu) and w.isVisible() and w is not active]
    for m in menus:
        for a in m.actions():
            if strip(a.text()) == text:
                p = m.mapToGlobal(m.actionGeometry(a).center())
                return p.x(), p.y()
    raise KeyError(text)


def modal(cls=None):
    d = QApplication.activeModalWidget()
    if d is None or (cls and not any(c.__name__ == cls for c in type(d).__mro__)):
        raise LookupError("no dialog yet")
    return d


def dialogs():
    """The open dialogs, the active modal one first."""
    from PySide6.QtWidgets import QDialog
    active = QApplication.activeModalWidget()
    rest = [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible() and w is not active]
    return ([active] if active else []) + rest


def modal_button(text):
    for d in dialogs():
        for b in d.findChildren(QAbstractButton):
            try:
                if strip(b.text()) == text and b.isVisible():
                    return centre(b)
            except RuntimeError:        # a button being deleted
                continue
    raise KeyError(text)


def modal_child(cls_name, n=0, dx=0, dy=0):
    found = [w for w in modal().findChildren(object) if type(w).__name__ == cls_name and w.isVisible()]
    return centre(found[n], dx, dy)


def button(parent, text):
    for b in parent.findChildren(QAbstractButton):
        if strip(b.text()) == text and b.isVisible():
            return centre(b)
    raise KeyError(text)


def tree_item(tree, path):
    from rdt.gui.variables import PATH_ROLE
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        if item.data(0, PATH_ROLE) == path or item.text(0) == path or item.text(0).endswith(path):
            return item
        it += 1
    raise KeyError(path)


def tree_pos(tree, path, col=0, dx=0):
    item = tree_item(tree, path)
    r = tree.visualItemRect(item)
    if r.height() == 0:
        raise LookupError(path)
    x = tree.header().sectionViewportPosition(col) + 30 + dx
    p = tree.viewport().mapToGlobal(QPoint(x, r.center().y()))
    return p.x(), p.y()


def top_item(tree, path):
    from rdt.gui.variables import PATH_ROLE
    for k in range(tree.topLevelItemCount()):
        if tree.topLevelItem(k).data(0, PATH_ROLE) == path:
            return tree.topLevelItem(k)
    raise KeyError(path)


def top_pos(tree, path):
    r = tree.visualItemRect(top_item(tree, path))
    p = tree.viewport().mapToGlobal(QPoint(tree.header().sectionViewportPosition(0) + 60, r.center().y()))
    return p.x(), p.y()


def row_button(tree, path, n_from_right):
    """The hover buttons at the right end of a top-level row's name column: 0 = remove, 1 = chart."""
    item = top_item(tree, path)
    r = tree.visualItemRect(item)
    h = tree.header()
    x = h.sectionViewportPosition(0) + h.sectionSize(0) - 11 - 20 * n_from_right
    p = tree.viewport().mapToGlobal(QPoint(x, r.center().y()))
    return p.x(), p.y()


def tab_pos(name):
    bar = W.watch_tabs.tabBar()
    for i in range(bar.count()):
        if strip(bar.tabText(i)) == name:
            return bar.mapToGlobal(bar.tabRect(i).center()).toTuple()
    raise KeyError(name)


def card(name):
    return W.symbols.cards[name]


# --- real input, with every position logged for the zoom -----------------------------------
FOCUS = []


def xdo(*args):
    subprocess.run(["xdotool", *map(str, args)], check=True)


def where():
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True).stdout
    v = dict(l.split("=") for l in out.split())
    return int(v["X"]), int(v["Y"])


def focus(xy):
    """Point the zoom at xy without moving the pointer, until the pointer next moves."""
    FOCUS.append((time.time(), xy[0], xy[1]))


def move(xy, secs=0.6):
    x0, y0 = where()
    x1, y1 = xy
    n = max(int(secs * 60), 2)
    for i in range(1, n + 1):
        t = i / n
        e = t * t * (3 - 2 * t)
        x, y = round(x0 + (x1 - x0) * e), round(y0 + (y1 - y0) * e)
        xdo("mousemove", x, y)
        FOCUS.append((time.time(), x, y))
        time.sleep(secs / n)
    time.sleep(0.15)


def click(xy=None, button=1, double=False, secs=0.6):
    if xy:
        move(xy, secs)
    if double:
        xdo("click", "--repeat", 2, "--delay", 90, button)
    else:
        xdo("click", button)
    time.sleep(0.35)


def ctrl_click(xy):
    move(xy)
    xdo("keydown", "ctrl")
    xdo("click", 1)
    xdo("keyup", "ctrl")
    time.sleep(0.35)


def drag(a, b, secs=1.0):
    move(a)
    xdo("mousedown", 1)
    time.sleep(0.2)
    move((a[0] + 12, a[1] + 8), 0.2)
    move(b, secs)
    time.sleep(0.6)
    move((b[0] + 3, b[1] + 2), 0.2)
    xdo("mouseup", 1)
    time.sleep(0.4)


def wheel(n, up=True):
    for _ in range(n):
        xdo("click", 4 if up else 5)
        time.sleep(0.12)


def typ(text, delay=75):
    xdo("type", "--delay", delay, text)
    time.sleep(0.3)


def key(*keys):
    xdo("key", *keys)
    time.sleep(0.3)


def popup_path(path, first_direct=True):
    for n, p in enumerate(path):
        time.sleep(0.45)
        x, y = ui(lambda p=p: popup_item(p))
        if n or not first_direct:   # in a submenu: across first, then down, so it stays open
            move((x, where()[1]), 0.35)
        click((x, y), secs=0.35)


def menu(*path):
    click(ui(lambda: menu_title(path[0])))
    popup_path(path[1:])


def context(xy, *path):
    click(xy, button=3)
    popup_path(path)


def hover(xy, pause=0.5):
    move(xy)
    time.sleep(pause)


def file_dialog(path):
    ui(lambda: modal("QFileDialog"))
    time.sleep(0.8)
    key("ctrl+a")
    typ(str(path), 28)
    time.sleep(0.4)
    key("Return")


def yes_if_asked(text="Yes"):
    time.sleep(0.7)
    try:
        xy = _ui(lambda: modal_button(text))
    except (LookupError, KeyError, RuntimeError):
        return
    click(xy)


def term(text, delay=40, wait=0.0):
    typ(text, delay)
    key("Return")
    time.sleep(wait)


# ==============================================================================================
# The script
# ==============================================================================================

# --- The main window ---------------------------------------------------------------------------
@step("This is RDT, the Remote Debugger Tool, open on the example workspace, with the simulator "
      "running on this PC.", "The main window")
def _():
    hover(ui(lambda: centre(W.watch_tabs)), 1.0)


@step("Across the top are the menus: File, Target, Watch, View and Help.", zoom=1.7)
def _():
    for m in ("File", "Target", "Watch", "View", "Help"):
        hover(ui(lambda m=m: menu_title(m)), 0.35)


@step("The toolbar starts with the panel buttons: Watch, Scope, Memory and Console.", zoom=1.8)
def _():
    for b in ("watch", "scope", "memory", "console"):
        hover(ui(lambda b=b: centre(W.modes.buttons[b])), 0.4)


@step("Then the link: the transport, and the IP address and port, or the COM port and baud rate.", zoom=1.8)
def _():
    for w in ("transport", "host", "port"):
        hover(ui(lambda w=w: centre(getattr(W.bar, w))), 0.5)


@step("The gear opens the SCPI profile, and the connect button shows the state of the link.", zoom=1.8)
def _():
    hover(ui(lambda: centre(W.bar.profile)), 0.7)
    hover(ui(lambda: centre(W.bar.connect_button)), 0.7)


@step("Then RUN and STOP for polling, the polling interval, Poll now, and Log CSV.", zoom=1.8)
def _():
    for w in ("polling", "poll", "poll_now", "log_button"):
        hover(ui(lambda w=w: centre(getattr(W.bar, w))), 0.45)


@step("On the left is the Target panel, with a card for each MCU. In the middle are the watch tabs, "
      "and along the bottom, the status bar.")
def _():
    hover(ui(lambda: centre(card("main"))), 1.0)
    hover(ui(lambda: centre(W.watch_tabs)), 1.0)
    hover(ui(lambda: centre(W.statusBar(), dx=600)), 0.6)


# --- Set up a bench -----------------------------------------------------------------------------
@step("To start from scratch, there’s File, New workspace, or File, Open. I’ll keep this one.",
      "Set up a bench", zoom=1.6)
def _():
    click(ui(lambda: menu_title("File")))
    time.sleep(0.6)
    m = ui(lambda: W.menu_bar.actions()[0].menu())
    for text in ("New workspace", "Open..."):
        hover(ui(lambda text=text: next(m.mapToGlobal(m.actionGeometry(a).center()).toTuple()
                                        for a in m.actions() if strip(a.text()) == text)), 0.8)
    key("Escape")


@step("The link can be changed while disconnected. Serial shows the COM port and baud rate. I’ll go back to TCP.",
      zoom=1.8)
def _():
    click(ui(lambda: centre(W.bar.transport)))
    time.sleep(0.4)
    key("Down")
    key("Down")
    key("Return")
    time.sleep(0.8)
    hover(ui(lambda: centre(W.bar.com)), 0.6)
    hover(ui(lambda: centre(W.bar.baud)), 0.6)
    click(ui(lambda: centre(W.bar.transport)))
    time.sleep(0.4)
    key("Up")
    key("Up")
    key("Return")


@step("The gear opens the SCPI profile: the read and write commands your firmware understands.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.bar.profile)))
    time.sleep(0.8)
    hover(ui(lambda: modal_child("QLineEdit", 1)), 0.6)
    hover(ui(lambda: modal_child("QLineEdit", 2)), 0.6)


@step("The preview shows each command exactly as it will be sent, with the address as eight hex digits.",
      zoom=1.7)
def _():
    hover(ui(lambda: centre(modal().preview)), 1.2)


@step("Load from file fills the fields from a profile file, and Test read tries the profile once connected.",
      zoom=1.6)
def _():
    hover(ui(lambda: centre(modal().load_button)), 0.9)
    hover(ui(lambda: centre(modal().test_button)), 0.9)


@step("Target, MCUs and ELF files lists every MCU.", zoom=1.4)
def _():
    click(ui(lambda: modal_button("Cancel")))
    time.sleep(0.4)
    menu("Target", "MCUs and ELF files...")


@step("Each has an id, a name, a short name and colour for its tag, its ELF file, and its own timeout.",
      zoom=1.6)
def _():
    time.sleep(0.5)
    t = ui(lambda: modal().table)
    for c in range(6):
        hover(ui(lambda c=c: t.viewport().mapToGlobal(t.visualRect(t.model().index(0, c)).center()).toTuple()),
              0.35)


@step("A double-click on a colour picks another one.", zoom=1.6)
def _():
    t = ui(lambda: modal().table)
    hover(ui(lambda: t.viewport().mapToGlobal(t.visualRect(t.model().index(1, 3)).center()).toTuple()), 1.0)


@step("Memory ranges come from a memory map file, with File, Import, Memory map.", zoom=1.3)
def _():
    click(ui(lambda: modal_button("Cancel")))
    time.sleep(0.4)
    menu("File", "Import", "Memory map...")


@step("I pick the file, and add its ranges to the ones I have.", zoom=1.4)
def _():
    file_dialog(FILES / "gpiob.memory.toml")
    time.sleep(0.6)
    click(ui(lambda: modal_button("Add")))


@step("Each card’s Memory ranges list shows what RDT may read and write on that MCU.", zoom=1.8)
def _():
    click(ui(lambda: centre(card("main").ranges_button)))
    time.sleep(0.4)
    hover(ui(lambda: centre(card("main").ranges)), 1.0)
    click(ui(lambda: centre(card("main").ranges_button)))


@step("Known commands for the console come in the same way, with File, Import, Commands.", zoom=1.3)
def _():
    menu("File", "Import", "Commands...")
    file_dialog(FILES / "sim.commands.toml")
    yes_if_asked("Add")


# --- Connect -------------------------------------------------------------------------------------
@step("Now I connect, with the button or F5.", "Connect", zoom=1.8)
def _():
    click(ui(lambda: centre(W.bar.connect_button)))


@step("It turns green. Connected means the socket is open; RDT sends nothing to test the device.", zoom=1.8)
def _():
    hover(ui(lambda: centre(W.bar.connect_button)), 1.0)


@step("Each card checks its ELF file against the firmware. The main MCU matches, but motorctl doesn’t.",
      zoom=1.8)
def _():
    hover(ui(lambda: centre(card("main").elf)), 1.2)
    hover(ui(lambda: centre(card("motorctl").elf)), 1.0)


@step("The red bar says so, and every read and write to motorctl is refused until it’s fixed.", zoom=1.6)
def _():
    hover(ui(lambda: centre(W.banner.text, dx=-200)), 1.2)


@step("Load ELF opens the MCU list. I select motorctl’s ELF, and press Browse.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.banner.load_button)))
    time.sleep(0.6)
    t = ui(lambda: modal().table)
    click(ui(lambda: t.viewport().mapToGlobal(t.visualRect(t.model().index(1, 4)).center()).toTuple()))
    click(ui(lambda: modal_button("Browse ELF...")))


@step("I pick the ELF that matches the firmware, and press OK.", zoom=1.4)
def _():
    file_dialog(BENCH_DIR / "firmware/motorctl.elf")
    time.sleep(0.6)
    click(ui(lambda: modal_button("OK")))


@step("Both cards are green now, and the red bar is gone.", zoom=1.6)
def _():
    time.sleep(1.0)
    hover(ui(lambda: centre(card("main").dot)), 0.8)
    hover(ui(lambda: centre(card("motorctl").elf)), 1.0)


@step("A right-click on a card checks its ELF again, edits the MCUs, or opens it in the memory view.",
      zoom=1.6)
def _():
    click(ui(lambda: centre(card("main").name)), button=3)
    time.sleep(0.4)
    for p in ("Check ELF again", "MCUs and ELF files...", "Open in the memory view"):
        hover(ui(lambda p=p: popup_item(p)), 0.45)
    key("Escape")


@step("If a secondary MCU stops answering, only its card turns silent. The other MCUs keep working.",
      zoom=1.6)
def _():
    hover(ui(lambda: centre(card("motorctl").link)), 0.3)
    SIM.device.handle("SIMulation:MCU:SILent 1,1")
    time.sleep(3.5)


@step("When it answers again, RDT picks it up by itself.", zoom=1.6)
def _():
    SIM.device.handle("SIMulation:MCU:SILent 1,0")
    time.sleep(2.5)


@step("If the whole board stops answering, the button turns red: link down. Writes are refused, and the "
      "last values stay in grey.", zoom=1.3)
def _():
    hover(ui(lambda: centre(W.bar.connect_button, dx=200, dy=120)), 0.2)
    SIM.device.handle("SIMulation:FREEze 9")
    time.sleep(5.5)


@step("When the replies come back, it goes green again.", zoom=1.3)
def _():
    time.sleep(4.5)


# --- Watch variables -----------------------------------------------------------------------------
@step("The Motor tab is live. Each row starts with its MCU’s tag, and a value that just changed flashes yellow.",
      "Watch variables", zoom=1.8)
def _():
    hover(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 1)), 1.5)


@step("Hovering over a name shows the full expression.", zoom=1.8)
def _():
    hover(ui(lambda: tree_pos(W.watch_tree, "main:motor.target", 0, 40)), 1.6)


@step("To add variables, I open the main card’s Variables, and type in the search box.", zoom=1.6)
def _():
    click(ui(lambda: centre(card("main").browser.search)))
    key("ctrl+a")
    typ("tick")


@step("A double-click adds a variable to the tab in front.", zoom=1.6)
def _():
    time.sleep(0.4)
    click(ui(lambda: tree_pos(card("main").browser.list, "tick_count", 0)), double=True)


@step("Or I select several, and press Add to watch list.", zoom=1.6)
def _():
    click(ui(lambda: centre(card("main").browser.search)))
    key("ctrl+a")
    key("BackSpace")
    time.sleep(0.4)
    click(ui(lambda: tree_pos(card("main").browser.list, "device_name", 0)))
    ctrl_click(ui(lambda: tree_pos(card("main").browser.list, "firmware_name", 0)))
    click(ui(lambda: centre(card("main").browser.add_button)))


@step("Variables can also be dragged onto a tab.", zoom=1.4)
def _():
    drag(ui(lambda: tree_pos(card("main").browser.list, "pid_gains", 0)), ui(lambda: tab_pos("System")))


@step("Watch, Add expression, or Control E, takes any expression, such as one cell of an array.", zoom=1.4)
def _():
    click(ui(lambda: tab_pos("Motor")))
    key("ctrl+e")
    time.sleep(0.6)
    typ("main:lookup[1][2]", 70)
    key("Return")


@step("Structures and arrays open to every field and cell. A closed one isn’t read, which keeps the link free.",
      zoom=1.5)
def _():
    click(ui(lambda: tab_pos("Structures")))
    time.sleep(0.5)
    click(ui(lambda: tree_pos(W.watch_tree, "main:imu", 0)), double=True)
    time.sleep(0.5)
    click(ui(lambda: tree_pos(W.watch_tree, "main:channels", 0)), double=True)
    time.sleep(0.5)
    click(ui(lambda: tree_pos(W.watch_tree, "main:channels[0]", 0)), double=True)


@step("Enums show their names, and char arrays show as text.", zoom=1.8)
def _():
    hover(ui(lambda: tree_pos(W.watch_tree, "main:channels[0].kind", 1)), 0.9)
    click(ui(lambda: tree_pos(W.watch_tree, "main:boards", 0)), double=True)
    time.sleep(0.4)
    click(ui(lambda: tree_pos(W.watch_tree, "main:boards[0]", 0)), double=True)
    time.sleep(0.4)
    hover(ui(lambda: tree_pos(W.watch_tree, "main:boards[0].name", 1)), 0.9)


@step("A right-click on a field can give it its own row.", zoom=1.6)
def _():
    context(ui(lambda: tree_pos(W.watch_tree, "main:imu.temp_c10", 0)), "Watch this on its own")


@step("Hovering over a row shows two small buttons: a wave to chart it, and a cross to remove it. "
      "I’ll remove this one.", zoom=2.0)
def _():
    hover(ui(lambda: top_pos(W.watch_tree, "main:imu.temp_c10")), 0.6)
    hover(ui(lambda: row_button(W.watch_tree, "main:imu.temp_c10", 1)), 1.0)
    click(ui(lambda: row_button(W.watch_tree, "main:imu.temp_c10", 0)))


# --- Watch tabs ----------------------------------------------------------------------------------
@step("Each tab is its own watch list. The plus button makes a new one.", "Watch tabs", zoom=1.6)
def _():
    click(ui(lambda: centre(W.watch_tabs.cornerWidget())))
    time.sleep(0.6)
    key("ctrl+a")
    typ("Tuning", 90)
    key("Return")


@step("A double-click on a tab renames it.", zoom=1.6)
def _():
    click(ui(lambda: tab_pos("Tuning")), double=True)
    time.sleep(0.6)
    key("ctrl+a")
    typ("Gains", 90)
    key("Return")


@step("Tabs can be dragged into a new order.", zoom=1.6)
def _():
    drag(ui(lambda: tab_pos("Gains")), ui(lambda: tab_pos("System")), 0.9)


@step("Also show in tab puts the same item in another tab. It stays one item, read once.", zoom=1.5)
def _():
    click(ui(lambda: tab_pos("Motor")))
    time.sleep(0.4)
    context(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 0)), "Also show in tab", "Gains")
    time.sleep(0.3)
    click(ui(lambda: tab_pos("Gains")))


@step("Control Page Down moves to the next tab, and every tab keeps polling, not only the one in front.")
def _():
    for _ in range(3):
        key("ctrl+Next")
        time.sleep(0.6)
    click(ui(lambda: tab_pos("Motor")))


# --- Item settings and writing -------------------------------------------------------------------
@step("A right-click on an item sets its format: decimal, hex, binary or char.", "Item settings", zoom=1.6)
def _():
    context(ui(lambda: tree_pos(W.watch_tree, "main:tick_count", 0)), "Format", "hex")


@step("Settings holds the fixed-point format, scale, offset and unit, the item’s polling, and its write safety.",
      zoom=1.4)
def _():
    context(ui(lambda: tree_pos(W.watch_tree, "main:motor.current_ma", 0)), "Settings...")
    time.sleep(0.6)
    for n in (0, 1, 2):
        hover(ui(lambda n=n: modal_child("QLineEdit", n)), 0.35)
    hover(ui(lambda: centre(modal().poll)), 0.5)
    hover(ui(lambda: centre(modal().read_only)), 0.5)


@step("This one shows milliamps as amps, with a scale of one thousandth.", zoom=1.8)
def _():
    hover(ui(lambda: centre(modal().scale)), 0.9)
    hover(ui(lambda: centre(modal().unit)), 0.9)
    click(ui(lambda: modal_button("Cancel")))


@step("To change a value, I double-click it, type the new value in its units, and press Enter.",
      "Change a value", zoom=1.8)
def _():
    click(ui(lambda: tree_pos(W.watch_tree, "main:motor.target", 1)), double=True)
    key("ctrl+a")
    typ("2500", 160)
    key("Return")


@step("This item asks before writing, so I confirm. RDT writes it, and reads it back to check.", zoom=1.6)
def _():
    time.sleep(0.5)
    click(ui(lambda: modal_button("Yes")))


@step("A value outside the item’s limits is refused, with the reason in the red bar.", zoom=1.5)
def _():
    click(ui(lambda: tree_pos(W.watch_tree, "main:motor.target", 1)), double=True)
    key("ctrl+a")
    typ("5000", 160)
    key("Return")
    yes_if_asked()
    hover(ui(lambda: centre(W.banner.text, dx=-300)), 0.8)


@step("Motor mode is read-only, so it’s grey. Watch, All items read-only, protects every item at once.",
      zoom=1.5)
def _():
    click(ui(lambda: centre(W.banner.close_button)))
    hover(ui(lambda: tree_pos(W.watch_tree, "main:motor.mode", 1)), 0.8)
    menu("Watch", "All items read-only")
    time.sleep(0.8)
    menu("Watch", "All items read-only")


# --- Polling -------------------------------------------------------------------------------------
@step("STOP, or F7, pauses all polling.", "Polling", zoom=1.8)
def _():
    click(ui(lambda: centre(W.bar.polling)))
    time.sleep(0.8)


@step("Poll now, or F6, reads everything once.", zoom=1.8)
def _():
    click(ui(lambda: centre(W.bar.poll_now)))
    time.sleep(0.6)


@step("The box beside RUN sets the global polling interval.", zoom=1.8)
def _():
    click(ui(lambda: centre(W.bar.poll)))
    key("ctrl+a")
    typ("250", 150)
    key("Return")


@step("Each item can follow it, have its own interval, or be read only by hand, with Read now.", zoom=1.6)
def _():
    context(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 0)), "Read now")


@step("RUN starts polling again. The Rate column shows how often each item is really read.", zoom=1.6)
def _():
    click(ui(lambda: centre(W.bar.polling)))
    time.sleep(0.8)
    hover(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 4)), 1.0)


# --- Chart ---------------------------------------------------------------------------------------
@step("The Scope button shows the chart.", "Chart")
def _():
    click(ui(lambda: centre(W.modes.buttons["scope"])))


@step("Add to chart puts a value on it, drawn in its MCU’s colour.", zoom=1.3)
def _():
    context(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 0)), "Add to chart")
    time.sleep(1.0)
    hover(ui(lambda: centre(W.chart)), 0.6)


@step("Or I select a row and press Control G, or drag rows onto the chart.", zoom=1.3)
def _():
    click(ui(lambda: tree_pos(W.watch_tree, "main:motor.current_ma", 0)))
    key("ctrl+g")
    time.sleep(0.6)
    click(ui(lambda: tab_pos("System")))
    time.sleep(0.4)
    drag(ui(lambda: tree_pos(W.watch_tree, "motorctl:temperature", 0)), ui(lambda: centre(W.chart, dx=-100)))
    click(ui(lambda: tab_pos("Motor")))


@step("Window sets how much time is shown.", zoom=1.7)
def _():
    click(ui(lambda: centre(W.chart.window)))
    time.sleep(0.4)
    key("Up")
    key("Return")


@step("Space pauses the view while the data keeps coming. The wheel zooms, and dragging pans.", zoom=1.3)
def _():
    click(ui(lambda: centre(W.chart, dx=-150)))
    key("space")
    time.sleep(0.4)
    wheel(4)
    time.sleep(0.3)
    drag(ui(lambda: centre(W.chart, dx=-150)), ui(lambda: centre(W.chart, dx=-350)), 0.8)


@step("Fit Y rescales, and Run follows the data again.", zoom=1.6)
def _():
    click(ui(lambda: button(W.chart, "Fit Y")))
    time.sleep(0.5)
    click(ui(lambda: centre(W.chart.pause_button)))


@step("Cursors show each signal’s value at two points, and the time between them.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.chart.cursor_button)))
    hover(ui(lambda: centre(W.chart, dx=-200)), 0.8)
    hover(ui(lambda: centre(W.chart.readout)), 0.8)


@step("A right-click on a signal moves it to another Y axis, changes its colour, or removes it.", zoom=1.6)
def _():
    t = ui(lambda: W.chart.list)
    click(ui(lambda: t.viewport().mapToGlobal(t.visualItemRect(t.topLevelItem(1)).center()).toTuple()),
          button=3)
    time.sleep(0.4)
    hover(ui(lambda: popup_item("Colour...")), 0.5)
    hover(ui(lambda: popup_item("Y axis")), 0.7)
    key("Escape")
    key("Escape")
    click(ui(lambda: centre(W.chart.cursor_button)))


@step("Control T adds minimum, maximum, mean and sample columns to the watch tree.", "Statistics", zoom=1.4)
def _():
    click(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 0)))
    key("ctrl+t")
    time.sleep(0.6)
    hover(ui(lambda: tree_pos(W.watch_tree, "main:motor.speed", 6)), 1.0)


@step("Log CSV, or Control L, writes every value to a file you can open in Excel.", "CSV log", zoom=1.5)
def _():
    click(ui(lambda: centre(W.bar.log_button)))
    file_dialog(BENCH_DIR / "run1.csv")
    time.sleep(0.5)
    hover(ui(lambda: centre(W.bar.log_button)), 0.5)


@step("Pressing it again stops the log.", zoom=1.8)
def _():
    time.sleep(1.0)
    click(ui(lambda: centre(W.bar.log_button)))
    key("ctrl+t")


# --- Memory view ---------------------------------------------------------------------------------
@step("The Memory button shows raw memory. I type a variable name and a length, and press Read.",
      "Memory view", zoom=1.4)
def _():
    click(ui(lambda: centre(W.modes.buttons["memory"])))
    click(ui(lambda: centre(W.memory.address)))
    key("ctrl+a")
    typ("motor", 110)
    click(ui(lambda: centre(W.memory.length)))
    key("ctrl+a")
    typ("64", 150)
    click(ui(lambda: button(W.memory, "Read")))


@step("Refresh reads it again at the polling interval.", zoom=1.7)
def _():
    click(ui(lambda: centre(W.memory.auto)))
    time.sleep(0.6)


@step("A right-click changes how cells show, such as signed shorts, in decimal.", zoom=1.4)
def _():
    context(ui(lambda: centre(W.memory.table, dx=-200, dy=-40)), "Signed", "Short")
    time.sleep(0.6)
    context(ui(lambda: centre(W.memory.table, dx=-200, dy=-40)), "Decimal")


@step("Or as floats, where the motor’s speed and target are easy to read.", zoom=1.5)
def _():
    context(ui(lambda: centre(W.memory.table, dx=-200, dy=-40)), "Float")
    time.sleep(0.6)


@step("A double-click on a cell writes a new value. This one is the target, so it asks first.", zoom=1.7)
def _():
    t = ui(lambda: W.memory.table)
    click(ui(lambda: t.viewport().mapToGlobal(t.visualRect(t.model().index(0, 1)).center()).toTuple()),
          double=True)
    key("ctrl+a")
    typ("1800", 150)
    key("Return")
    yes_if_asked()
    click(ui(lambda: centre(W.memory.auto)))


# --- SCPI console --------------------------------------------------------------------------------
@step("The Console button opens a terminal to the firmware. I type a query and press Enter.",
      "SCPI console", zoom=1.6)
def _():
    click(ui(lambda: centre(W.modes.buttons["console"])))
    time.sleep(0.4)
    click(ui(lambda: centre(W.console.output)))
    focus(ui(lambda: W.console.output.mapToGlobal(W.console.output.cursorRect().center()).toTuple()))
    term("*IDN?", 90)


def prompt_focus():
    focus(ui(lambda: W.console.output.mapToGlobal(W.console.output.cursorRect().center()).toTuple()))


@step("A failed command gets no reply at all. The error query reads why from the firmware.", zoom=1.6)
def _():
    prompt_focus()
    term("MEMory:READ? 0,00000000,4", 50)
    time.sleep(1.0)
    prompt_focus()
    term("SYSTem:ERRor?", 70)


@step("As I type, matching commands appear, with the best one in grey. The right arrow takes it.", zoom=1.6)
def _():
    prompt_focus()
    typ("SIMulation:MCU:L", 110)
    time.sleep(1.0)
    key("Right")
    time.sleep(0.6)
    key("Return")


@step("Up walks back through the history.", zoom=1.6)
def _():
    prompt_focus()
    key("Up")
    time.sleep(0.5)
    key("Up")
    time.sleep(0.5)
    key("Return")


@step("Commands lists the known commands I imported. A double-click puts one on the prompt line.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.console.commands_button)))
    time.sleep(0.5)
    t = ui(lambda: W.console.commands.tree)
    click(ui(lambda: tree_pos(t, "SIMulation:MCU:SILent? 1", 0)), double=True)
    prompt_focus()
    key("Return")


@step("History lists what I sent, to send again.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.console.commands_button)))
    click(ui(lambda: centre(W.console.history_button)))
    time.sleep(0.5)
    hover(ui(lambda: centre(W.console.history_panel.list)), 1.0)
    click(ui(lambda: centre(W.console.history_button)))


@step("Control F searches the transcript, and Control plus and minus change the text size.", zoom=1.4)
def _():
    click(ui(lambda: centre(W.console.output)))
    key("ctrl+f")
    typ("IDN", 120)
    time.sleep(0.8)
    key("Escape")
    click(ui(lambda: centre(W.console.output)))
    key("ctrl+equal")
    key("ctrl+equal")
    time.sleep(0.6)
    key("ctrl+minus")
    key("ctrl+minus")


@step("Timestamps and Wrap change the view, and Log to file saves the session to a text file.", zoom=1.6)
def _():
    click(ui(lambda: centre(W.console.timestamps)))
    time.sleep(0.6)
    click(ui(lambda: centre(W.console.timestamps)))
    hover(ui(lambda: centre(W.console.wrap)), 0.5)
    hover(ui(lambda: centre(W.console.log_button)), 0.6)


@step("A right-click sends a break on a COM port, or turns a selection into hex.", zoom=1.5)
def _():
    click(ui(lambda: centre(W.console.output, dx=-100)), button=3)
    time.sleep(0.5)
    hover(ui(lambda: popup_item("Send break (250 ms)")), 0.8)
    key("Escape")


# --- Layout --------------------------------------------------------------------------------------
@step("The panel buttons, or Control 1 to 4, show and hide their panels.", "Layout")
def _():
    for b in ("console", "memory", "scope"):
        click(ui(lambda b=b: centre(W.modes.buttons[b])))
        time.sleep(0.5)
    click(ui(lambda: centre(W.modes.buttons["scope"])))


@step("Drag a panel by its title to dock it somewhere else, stack it, or float it.")
def _():
    d = ui(lambda: W.docks["Chart"])
    a = ui(lambda: d.mapToGlobal(QPoint(70, 10)).toTuple())
    drag(a, ui(lambda: centre(W.watch_tabs, dx=350, dy=-100)), 1.2)
    time.sleep(0.8)


@step("View, Target hides the MCU cards. Save layout keeps the arrangement, and Reset layout puts every "
      "panel back.")
def _():
    menu("View", "Target")
    time.sleep(0.8)
    menu("View", "Target")
    time.sleep(0.4)
    menu("View", "Reset layout")


@step("View, Density switches between compact and comfortable rows.", zoom=1.3)
def _():
    menu("View", "Density", "Comfortable rows")
    time.sleep(1.2)
    menu("View", "Density", "Compact rows")


# --- Themes --------------------------------------------------------------------------------------
@step("View, Theme changes the look. Here’s Islands, with rounded panels.", "Themes")
def _():
    menu("View", "Theme", "Islands")
    time.sleep(0.8)


@step("Instrument, made for scope work.")
def _():
    menu("View", "Theme", "Instrument")
    time.sleep(0.8)


@step("Light pro.")
def _():
    menu("View", "Theme", "Light pro")
    time.sleep(1.2)


@step("And back to Studio dark.")
def _():
    menu("View", "Theme", "Studio dark")


# --- Save, import and export ---------------------------------------------------------------------
@step("A star in the title means unsaved changes. Control S saves the workspace and its files.",
      "Save, import and export", zoom=1.6)
def _():
    hover((960, 14), 1.0)
    key("ctrl+s")
    time.sleep(0.8)


@step("File, Export saves one part at a time, such as the watch list, to share it.", zoom=1.3)
def _():
    menu("File", "Export", "Watch list...")
    file_dialog(FILES / "motor.watch.toml")


@step("File, Import brings it into another workspace, adding to its tabs or replacing them. Here I’ll cancel.",
      zoom=1.3)
def _():
    menu("File", "Import", "Watch list...")
    file_dialog(FILES / "motor.watch.toml")
    time.sleep(1.0)
    click(ui(lambda: modal_button("Cancel")))


@step("Interface settings, with your theme and console setup, export the same way.", zoom=1.3)
def _():
    menu("File", "Export", "Interface settings...")
    file_dialog(FILES / "my-ui.toml")


# --- Help ----------------------------------------------------------------------------------------
@step("Help has the user manual, the release notes, the update check, and the keyboard shortcuts.",
      "Help", zoom=1.5)
def _():
    click(ui(lambda: menu_title("Help")))
    time.sleep(0.4)
    for p in ("User manual", "Release notes", "Check for updates", "Keyboard shortcuts"):
        hover(ui(lambda p=p: popup_item(p)), 0.45)
    key("Escape")


@step("F1 lists every shortcut.", zoom=1.2)
def _():
    key("F1")
    time.sleep(2.2)
    key("Escape")


# --- Command line --------------------------------------------------------------------------------
TERM_CENTRE = (960, 560)


@step("Everything also works from the command line, on the same workspace.", "Command line")
def _():
    subprocess.Popen(["xterm", "-fa", "JetBrains Mono", "-fs", "14", "-geometry", "118x32+250+120",
                      "-bg", "#11161D", "-fg", "#D6DEE6", "-cr", "#3DBE8B", "-title", "Terminal",
                      "-e", "bash", "--norc", "-i"], cwd=BENCH_DIR,
                     env={**os.environ, "PS1": "$ ", "QT_QPA_PLATFORM": "offscreen"})
    time.sleep(1.5)
    click(TERM_CENTRE)


@step("rdt info shows the link, the profile, and each MCU’s ELF check and memory map.", zoom=1.25)
def _():
    focus(TERM_CENTRE)
    term("rdt -w sim_bench.rdtw info", wait=2.5)


@step("symbols and ptype look up variables and their types, with no device needed.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw symbols main 'motor*'", wait=2.5)
    time.sleep(0.6)
    term("rdt -w sim_bench.rdtw ptype main:motor", wait=2.5)


@step("read opens a structure to every field. With a tab name, it reads that tab’s items.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw read main:motor", wait=2.5)
    time.sleep(0.6)
    term("rdt -w sim_bench.rdtw read --tab Motor", wait=2.5)


@step("write changes one value, and confirms it by reading it back.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw write main:motor.target 2000 -y", wait=2.5)


@step("dump shows memory as hex and ASCII.", zoom=1.25)
def _():
    term("rdt -w sim_bench.rdtw dump main:device_name", wait=2.5)


@step("watch streams values, and can log them to a CSV file.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw watch main:motor.speed motorctl:temperature -i 250 -n 12 --csv run2.csv")
    time.sleep(3.5)


@step("send sends one SCPI command. With expect, the exit code says whether the reply matched.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw send '*IDN?' --expect '^RDT,'; echo exit code $?", wait=2.5)


@step("rdt console is the same console, in the terminal, with RDT’s own dot commands.", zoom=1.25)
def _():
    term("rdt -w sim_bench.rdtw console")
    time.sleep(2.5)
    term(".read main:motor.speed")
    time.sleep(1.2)
    term(".quit")


@step("add, list and tab edit a workspace without a device.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw tab add Tuning")
    term("rdt -w sim_bench.rdtw add main:pid_gains --tab Tuning")
    term("rdt -w sim_bench.rdtw list", wait=2.5)


@step("And memory, watchlist and commands import and export parts of it, as in the GUI.", zoom=1.25)
def _():
    term("clear")
    term("rdt -w sim_bench.rdtw memory export h7.memory.toml && cat h7.memory.toml", wait=2.5)
    time.sleep(1.0)


# --- run -------------------------------------------------------------------------------------
def make_voice():
    k = kokoro()
    durs = []
    for i, (_, text, _, _) in enumerate(STEPS):
        f, txt = OUT / "say" / f"{i:03d}.wav", OUT / "say" / f"{i:03d}.txt"
        if f.exists() and txt.exists() and txt.read_text() == text:
            durs.append(sf.info(str(f)).duration)
            continue
        txt.write_text(text)
        samples, sr = k.create(spoken(text), voice=VOICE, speed=1.0, lang="en-us")
        sf.write(f, samples, sr)
        durs.append(len(samples) / sr)
    return durs


def driver(durs, log, errors):
    rec = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
                            "-f", "x11grab", "-draw_mouse", "1", "-framerate", "30", "-video_size", "1920x1080",
                            "-i", os.environ["DISPLAY"], "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
                            "-copyts", str(OUT / "screen.mkv")], stdin=subprocess.PIPE)
    time.sleep(2.5)
    try:
        for i, (chapter, text, zoom, fn) in enumerate(STEPS):
            t = time.time()
            log.append({"i": i, "t": t, "dur": durs[i], "text": text, "chapter": chapter, "zoom": zoom})
            print(f"{i:03d} {text}", flush=True)
            try:
                fn()
            except Exception:
                errors.append((i, traceback.format_exc()))
                print(f"!!! step {i} failed\n{traceback.format_exc()}", flush=True)
                for _ in range(3):              # close whatever the failure left open
                    key("Escape")
            log[-1]["end"] = time.time()
            left = t + durs[i] + 0.45 - time.time()
            if left > 0:
                time.sleep(left)
        time.sleep(1.5)
    finally:
        rec.stdin.write(b"q")
        rec.stdin.flush()
        rec.wait(30)
        (OUT / "steps.json").write_text(json.dumps(log, indent=1))
        (OUT / "focus.json").write_text(json.dumps(FOCUS))
        (OUT / "errors.json").write_text(json.dumps(errors, indent=1))
        subprocess.run(["pkill", "-f", "xterm"])
        ui(lambda: QApplication.instance().quit())


def fresh_bench():
    """The bench from bench/, with the simulator's ELFs; motorctl starts on an old build (one .text
    byte changed) so the walkthrough can show the ELF mismatch and then fix it."""
    from elftools.elf.elffile import ELFFile
    shutil.rmtree(BENCH_DIR, ignore_errors=True)
    shutil.copytree(TEMPLATE, BENCH_DIR)
    fw = BENCH_DIR / "firmware"
    fw.mkdir()
    for name in ("main.elf", "motorctl.elf"):
        shutil.copy(ROOT / "sim/examples/firmware" / name, fw / name)
    data = bytearray((fw / "motorctl.elf").read_bytes())
    with open(fw / "motorctl.elf", "rb") as f:
        off = ELFFile(f).get_section_by_name(".text")["sh_offset"]
    data[off + 8] ^= 0xFF
    (fw / "motorctl_old.elf").write_bytes(bytes(data))


def main():
    global W, SIM
    durs = make_voice()
    print(f"narration {sum(durs):.0f} s over {len(STEPS)} steps", flush=True)
    if "--voice-only" in sys.argv:
        return
    fresh_bench()
    app = QApplication([])
    from rdt.gui.app import MainWindow
    from sim import Simulator, TransportConfig, load_config
    cfg = load_config(ROOT / "sim/examples/two_mcus.toml")
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:5025")) as SIM:
        W = MainWindow()
        W.open_path(BENCH)
        W.set_theme("studio")
        for p in ("scope", "memory", "console"):
            W.toggle_panel(p, False)
        W.showMaximized()
        timer = QTimer()
        timer.timeout.connect(_run_jobs)
        timer.start(15)
        log, errors = [], []
        threading.Thread(target=driver, args=(durs, log, errors), daemon=True).start()
        app.exec()
        W.dirty = False


if __name__ == "__main__":
    main()
