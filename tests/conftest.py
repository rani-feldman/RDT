import os
import sys
from pathlib import Path

import pytest

# Qt's offscreen platform on Windows finds no fonts unless told where they are, and every font then
# looks proportional; point it at the Windows fonts before any test makes a QApplication.
if sys.platform == "win32" and os.environ.get("WINDIR"):
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ["WINDIR"], "Fonts"))

from sim import Device, Memory, Mcu, Profile, Region

FIRMWARE = Path(__file__).resolve().parent.parent / "sim" / "examples" / "firmware"


@pytest.fixture
def two_mcus():
    """MCU 0 and MCU 1 loaded from the example ELFs, with no signals so reads are stable."""
    mcus = []
    for mcu_id, name in ((0, "main"), (1, "motorctl")):
        mem = Memory()
        order = mem.load_elf(FIRMWARE / f"{name}.elf")
        mcus.append(Mcu(mcu_id, name, mem, order))
    return mcus


@pytest.fixture
def device(two_mcus):
    return Device(two_mcus, Profile(), internal_timeout_ms=1)


def ram_mcu(mcu_id=0, start=0x20000000, size=256, **kw):
    mem = Memory()
    mem.add(Region("RAM", start, bytearray(size), True))
    return Mcu(mcu_id, f"mcu{mcu_id}", mem, **kw)


@pytest.fixture(autouse=True)
def _symbol_cache(tmp_path_factory, monkeypatch):
    """Keep the DWARF cache out of the user's cache folder; shared by the whole test run."""
    monkeypatch.setenv("RDT_CACHE_DIR", str(tmp_path_factory.getbasetemp() / "symbol-cache"))


@pytest.fixture(autouse=True)
def _console_history(tmp_path, monkeypatch):
    """Each test gets its own console history file, never the user's."""
    monkeypatch.setenv("RDT_HISTORY_FILE", str(tmp_path / "console_history.txt"))
