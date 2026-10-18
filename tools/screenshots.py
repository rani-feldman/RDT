"""Screenshots of the real RDT window, connected to the simulator, with no display needed.

Used for the user manual's pictures and for showing a GUI change before it is merged. It starts the
simulator in this process on free ports, opens a workspace pointed at it, connects, lets a few polls
run, and saves the window once per theme. Your own ui.toml is not touched.

    python tools/screenshots.py OUT_DIR [--theme light --theme studio ...] [--panel memory ...]
        [--workspace examples/sim_bench.rdtw] [--size 1500x860] [--seconds 2] [--name main-window]

Each picture is OUT_DIR/<name>-<theme>.png. --panel shows a panel (watch, scope, memory, console)
in addition to the ones the workspace shows.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RDT_UI_FILE"] = str(Path(tempfile.mkdtemp()) / "ui.toml")
os.environ["RDT_HISTORY_FILE"] = str(Path(tempfile.mkdtemp()) / "console_history.txt")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.02)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path, help="folder for the PNG files")
    p.add_argument("--theme", action="append", help="theme name, repeatable (default: light)")
    p.add_argument("--panel", action="append", default=[], help="panel to show: watch, scope, memory, console")
    p.add_argument("--workspace", type=Path, default=ROOT / "examples" / "sim_bench.rdtw")
    p.add_argument("--sim-config", type=Path, default=ROOT / "sim" / "examples" / "two_mcus.toml")
    p.add_argument("--size", default="1500x860", help="window size, WIDTHxHEIGHT")
    p.add_argument("--seconds", type=float, default=2.0, help="how long to poll before each picture")
    p.add_argument("--name", default="main-window", help="file name before -<theme>.png")
    args = p.parse_args(argv)

    app = QApplication([])
    from rdt.gui import style
    from rdt.gui.app import MainWindow
    from rdt.workspace import Workspace
    from sim import Simulator, TransportConfig, load_config

    themes = args.theme or ["light"]
    unknown = [t for t in themes if t not in style.THEME]
    if unknown:
        p.error(f"unknown theme {', '.join(unknown)}; choose from {', '.join(style.THEME)}")
    width, height = (int(n) for n in args.size.lower().split("x"))
    args.out.mkdir(parents=True, exist_ok=True)

    cfg = load_config(args.sim_config)
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0")) as sim:
        ws = Workspace.load(args.workspace)
        ws.config.connection.address = "%s:%d" % sim.tcp_address
        bench = Path(tempfile.mkdtemp()) / args.workspace.name
        ws.save(bench)
        for name in themes:
            w = MainWindow()
            w.resize(width, height)
            w.open_path(bench)
            w.set_theme(name)
            for panel in args.panel:
                w.toggle_panel(panel, True)
            w.show()
            w.connect()
            pump(app, args.seconds)
            path = args.out / f"{args.name}-{name}.png"
            w.grab().save(str(path))
            print(path)
            w.dirty = False
            w.close()
            pump(app, 0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
