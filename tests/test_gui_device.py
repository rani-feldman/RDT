"""The GUI's device worker: cancelling a connect, and shutting down during a long job (H4, M5, M7)."""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
pytest.importorskip("pytestqt")

from rdt.gui import device as devmod  # noqa: E402


class SlowTarget:
    """Stands in for Target: connect() takes a while, and close() is counted."""
    made: list["SlowTarget"] = []

    def __init__(self, config, auto_reconnect=True):
        self.config = config
        self.closed = 0
        SlowTarget.made.append(self)

    def connect(self):
        time.sleep(0.3)

    def close(self):
        self.closed += 1


@pytest.fixture
def dev(qtbot, monkeypatch):
    SlowTarget.made = []
    monkeypatch.setattr(devmod, "Target", SlowTarget)
    d = devmod.Device()
    yield d
    d.shutdown()


def settle(qtbot, d):
    qtbot.waitUntil(lambda: d.pending == 0, timeout=5000)
    qtbot.wait(50)


def test_cancelling_a_connect_closes_its_target(qtbot, dev):
    opened, errors, closed = [], [], []
    dev.opened.connect(opened.append)
    dev.closed.connect(lambda: closed.append(1))
    dev.open(object(), errors.append)
    qtbot.wait(50)                 # the connect is running
    dev.close()                    # F5 again
    settle(qtbot, dev)
    assert dev.target is None and opened == [] and errors == [] and closed == [1]
    assert [t.closed for t in SlowTarget.made] == [1]
    assert dev._worker.target is None


def test_a_second_open_replaces_the_first(qtbot, dev):
    opened = []
    dev.opened.connect(opened.append)
    dev.open("first")
    qtbot.wait(50)
    dev.open("second")             # another workspace connects
    settle(qtbot, dev)
    assert [t.config for t in opened] == ["second"]
    assert dev.target is opened[0] and SlowTarget.made[0].closed == 1 and opened[0].closed == 0


def test_a_cancelled_connect_that_fails_says_nothing(qtbot, dev, monkeypatch):
    def fail(self):
        time.sleep(0.2)
        raise OSError("refused")
    monkeypatch.setattr(SlowTarget, "connect", fail)
    errors = []
    dev.open(object(), errors.append)
    qtbot.wait(50)
    dev.close()
    settle(qtbot, dev)
    assert errors == [] and dev.target is None


def test_shutdown_closes_the_port_after_the_running_job(qtbot, dev):
    dev.open(object())
    qtbot.waitUntil(lambda: dev.target is not None, timeout=5000)
    t = dev.target
    dev.submit(lambda _: time.sleep(0.3))
    ran = []
    dev.submit(lambda _: ran.append(1))   # still queued at shutdown: skipped
    qtbot.wait(50)
    dev.shutdown()
    assert t.closed == 1 and ran == [] and not dev._thread.isRunning()


def test_shutdown_during_a_long_job_does_not_abort(qtbot, dev):
    dev.open(object())
    qtbot.waitUntil(lambda: dev.target is not None, timeout=5000)
    t = dev.target
    release = threading.Event()
    dev.submit(lambda _: release.wait(5))
    qtbot.wait(50)
    dev.shutdown(wait_ms=100)              # gives up waiting, keeps the thread until exit
    assert dev._thread.isRunning() and (dev._thread, dev._worker) in devmod._left_running
    release.set()
    devmod._wait_left_running()            # what runs at exit
    assert not dev._thread.isRunning() and t.closed == 1


def test_garbage_is_collected_on_the_gui_thread(qtbot, dev):
    """Widgets freed by a collection on the worker thread crash Qt, so the GUI thread collects."""
    import gc
    assert not gc.isenabled() and devmod._gc_timer is not None and devmod._gc_timer.isActive()
    junk = []
    junk.append(junk)
    del junk
    before = gc.get_count()[0]
    for _ in range(gc.get_threshold()[0] + 10):
        x = []
        x.append(x)
    assert gc.get_count()[0] > before
    qtbot.waitUntil(lambda: gc.get_count()[0] < gc.get_threshold()[0], timeout=3000)
