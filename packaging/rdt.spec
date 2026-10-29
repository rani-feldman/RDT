# PyInstaller build of RDT (INS-1, INS-6): one folder with rdt-gui.exe (the window, no console),
# rdt.exe (the command line) and rdt-sim.exe (the simulator), sharing Python and every package in
# _internal. Run from the repository root through tools/build_release.ps1, or:
#     python packaging/version_info.py 1.2.0 build/version_info.txt
#     pyinstaller --noconfirm --distpath dist --workpath build/pyinstaller packaging/rdt.spec
import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "rdt" / "gui" / "assets" / "rdt.ico")
VERSION_FILE = os.environ.get("RDT_VERSION_FILE", str(ROOT / "build" / "version_info.txt"))
EXCLUDES = ["tkinter", "pytest", "pytestqt", "IPython"]

# the files RDT reads at run time besides its code (rdt.appfiles finds the docs here)
datas = [
    (str(ROOT / "rdt" / "gui" / "assets"), "rdt/gui/assets"),
    (str(ROOT / "sim" / "examples"), "sim/examples"),
    (str(ROOT / "CHANGELOG.md"), "docs"),
    (str(ROOT / "LICENSE"), "docs"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "docs"),
    (str(ROOT / "docs" / "USER_MANUAL.md"), "docs"),
    (str(ROOT / "docs" / "images"), "docs/images"),
    # the presentation (INS-17); the video stays in the repository, Help links to it (rani, 2026-10-29)
    (str(ROOT / "docs" / "media" / "rdt-presentation.pdf"), "docs/media"),
]
# each package's licence files, as THIRD_PARTY_NOTICES.md promises
for package in ("PySide6", "shiboken6", "pyqtgraph", "numpy", "pyelftools", "pyserial"):
    datas += copy_metadata(package)
if os.name == "nt":
    datas += copy_metadata("pyreadline3")


def analysis(script, extra_datas=()):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=list(extra_datas),
                    excludes=EXCLUDES, noarchive=False)


def exe(a, name, console):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,
               icon=ICON, version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
               upx=False, contents_directory="_internal")


gui = analysis("entry_gui.py", datas)
cli = analysis("entry_cli.py")
sim = analysis("entry_sim.py")

coll = COLLECT(
    exe(gui, "rdt-gui", console=False), gui.binaries, gui.datas,
    exe(cli, "rdt", console=True), cli.binaries, cli.datas,
    exe(sim, "rdt-sim", console=True), sim.binaries, sim.datas,
    name="RDT", upx=False,
)
