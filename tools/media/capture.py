"""Stills and clips of RDT driving the simulator, for the manual deck and video.

python tools/media/capture.py [THEME]  -> WORK/cap/stills/*.png and WORK/cap/clips/<name>/f00000.jpg at 10 fps
"""
import os, sys, tempfile, time
from pathlib import Path

from common import ROOT, WORK

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RDT_UI_FILE"] = str(Path(tempfile.mkdtemp()) / "ui.toml")
os.environ["RDT_HISTORY_FILE"] = str(Path(tempfile.mkdtemp()) / "console_history.txt")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest

OUT = WORK / "cap"
THEME = sys.argv[1] if len(sys.argv) > 1 else "studio"
W, H = 1600, 900
FPS = 10
app = QApplication([])

from rdt.gui import style
from rdt.gui.app import MainWindow
from rdt.gui.panels import ProfileDialog, McuDialog
from rdt.gui.variables import WatchDialog
from rdt.workspace import Workspace
from sim import Simulator, TransportConfig, load_config

(OUT / "stills").mkdir(parents=True, exist_ok=True)
(OUT / "clips").mkdir(parents=True, exist_ok=True)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.01)


class Rec:
    """Grabs the window at FPS into one clip folder while scripted actions run."""
    def __init__(self, w, name):
        self.w, self.dir, self.n = w, OUT / "clips" / name, 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self.next = time.monotonic()

    def run(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents(QEventLoop.AllEvents, 10)
            if time.monotonic() >= self.next:
                self.w.grab().save(str(self.dir / f"f{self.n:05d}.jpg"), "JPG", 92)
                with open(self.dir / "times.txt", "a") as f:
                    f.write(f"{time.monotonic():.3f}\n")
                self.n += 1
                self.next += 1 / FPS
            time.sleep(0.005)

    def type(self, text, per_char=0.06):
        for ch in text:
            QTest.keyClicks(app.focusWidget(), ch)
            self.run(per_char)


def still(widget, name):
    p = OUT / "stills" / f"{name}.png"
    widget.grab().save(str(p))
    print(p)


def window(bench, panels=()):
    w = MainWindow()
    w.resize(W, H)
    w.open_path(bench)
    w.set_theme(THEME)
    for p in ("scope", "memory", "console"):
        w.toggle_panel(p, p in panels)
    w.show()
    pump(0.3)
    return w


def close(w):
    w.dirty = False
    w.close()
    pump(0.3)


cfg = load_config(ROOT / "sim/examples/two_mcus.toml")
with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0")) as sim:
    tmp = WORK / "capture-bench"
    if tmp.exists():
        import shutil; shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    ws = Workspace.load(ROOT / "examples/sim_bench.rdtw")
    ws.config.connection.address = "%s:%d" % sim.tcp_address
    import shutil
    (tmp / "firmware").mkdir()
    for mcu in ws.config.mcus:
        src = Path(mcu.elf) if Path(mcu.elf).is_absolute() else (ROOT / "examples" / mcu.elf).resolve()
        shutil.copy(src, tmp / "firmware" / src.name)
        mcu.elf = str(tmp / "firmware" / src.name)
    bench = tmp / "sim_bench.rdtw"
    ws.save(bench)

    # 1. connect: disconnected, then connected with values moving
    w = window(bench)
    still(w, "disconnected")
    r = Rec(w, "connect")
    r.run(1.5)
    w.connect()
    r.run(5)
    still(w, "watch")
    # System tab
    w.watch_tabs.setCurrentIndex(1) if hasattr(w.watch_tabs, "setCurrentIndex") else None
    r.run(0.1)
    close(w)

    # 1b. structures and arrays
    w = window(bench)
    w.connect()
    pump(1.5)
    w.watch_tabs.setCurrentIndex(2)
    pump(0.5)
    tree = w.watch_tree
    r = Rec(w, "structures")
    r.run(1.5)
    for expr in ("main:imu", "main:channels"):
        st = tree.states.get(expr)
        if st:
            tree.expandItem(st.nodes[expr])
            r.run(1.2)
    st = tree.states.get("main:channels")
    if st:
        for path, node in st.nodes.items():
            if path.endswith("[0]"):
                tree.expandItem(node)
                break
    r.run(3)
    still(w, "structures")
    close(w)

    # 2. write a value in place
    w = window(bench)
    w.connect()
    pump(2)
    tree = w.watch_tree
    st = tree.states["main:motor.target"]
    item = st.nodes["main:motor.target"]
    r = Rec(w, "write")
    r.run(1.5)
    tree.setCurrentItem(item)
    tree._double_clicked(item, 1)
    r.run(0.6)
    ed = app.focusWidget()
    ed.selectAll()
    r.type("2500", 0.15)
    r.run(0.5)
    QTest.keyClick(app.focusWidget(), Qt.Key_Return)
    r.run(3)
    still(w, "write")
    close(w)

    # 3. chart, statistics
    w = window(bench, panels=("scope",))
    w.connect()
    pump(1)
    w.add_to_chart("main:motor.speed", "rpm")
    w.add_to_chart("motorctl:temperature", "degC")
    w.watch_tabs.show_stats(True)
    r = Rec(w, "chart")
    r.run(9)
    still(w, "chart")
    close(w)

    # 4. memory view
    w = window(bench, panels=("memory",))
    w.connect()
    pump(1.5)
    w.show_memory("main")
    r = Rec(w, "memory")
    r.run(1)
    m = w.memory
    m.address.setText("")
    m.address.setFocus()
    r.type("motor", 0.12)
    m.length.setValue(64)
    r.run(0.4)
    m.read()
    r.run(2)
    m.auto.setChecked(True)
    r.run(3)
    still(w, "memory")
    close(w)

    # 5. console
    w = window(bench, panels=("console",))
    w.connect()
    pump(1.5)
    w.toggle_panel("console", True)
    c = w.console
    term = c.output
    r = Rec(w, "console")
    r.run(1)
    if term is not None:
        term.setFocus()
    for cmd in ("*IDN?", "MEMory:READ? 0,20000000,16", "SYST:ERR?"):
        r.type(cmd, 0.05)
        r.run(0.4)
        QTest.keyClick(app.focusWidget(), Qt.Key_Return)
        r.run(1.5)
    r.type("MEM", 0.15)
    r.run(2)
    still(w, "console")
    close(w)

    # 6. dialogs
    w = window(bench)
    w.connect()
    pump(1.5)
    d = ProfileDialog(w.ws.config.profile, w.device, w, w.ws.config.profile_path)
    d.show(); pump(0.5); still(d, "dlg-profile"); d.close()
    d = McuDialog(w.ws.config.mcus, w)
    d.resize(1000, 320); d.show(); pump(0.5); still(d, "dlg-mcus"); d.close()
    d = WatchDialog(w.watch_tree.states["main:motor.target"].watch, w)
    d.show(); pump(0.5); still(d, "dlg-item"); d.close()
    d = w.show_shortcuts(); pump(0.5); still(d, "dlg-shortcuts"); d.close()
    close(w)

    # 7. ELF mismatch: motorctl given main's ELF
    ws2 = Workspace.load(bench)
    from elftools.elf.elffile import ELFFile
    src = tmp / "firmware" / "motorctl.elf"
    data = bytearray(src.read_bytes())
    with open(src, "rb") as f:
        off = ELFFile(f).get_section_by_name(".text")["sh_offset"]
    data[off + 8] ^= 0xFF
    (tmp / "firmware" / "motorctl_old.elf").write_bytes(bytes(data))
    for mcu in ws2.config.mcus:
        if mcu.name == "motorctl":
            mcu.elf = str(tmp / "firmware" / "motorctl_old.elf")
    bad = tmp / "bad" / "sim_bench.rdtw"
    bad.parent.mkdir()
    ws2.save(bad)
    w = window(bad)
    w.connect()
    pump(10)
    still(w, "mismatch")
    close(w)

    # 8. themes
    for t in style.THEME:
        w = MainWindow(); w.resize(W, H); w.open_path(bench); w.set_theme(t)
        w.toggle_panel("scope", True); w.show(); w.connect(); pump(0.3)
        w.add_to_chart("main:motor.speed", "rpm")
        pump(3)
        still(w, f"theme-{t}")
        close(w)
