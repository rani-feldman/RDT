"""The device link for the GUI: every access runs on one worker thread, in order.

Panels call ``submit(fn, on_done, on_error)``; ``fn(target)`` runs on the worker and the
callbacks run back on the GUI thread. A job with a ``key`` is dropped while an earlier job with
the same key is still waiting, so a poll that can't keep up falls behind instead of queueing.
"""

from __future__ import annotations

import atexit
import gc
import itertools
import logging
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot

from ..errors import ConfigError, LinkError
from ..session import LinkState
from ..target import Target, TargetConfig

log = logging.getLogger(__name__)


GC_MS = 500   # how often the GUI thread collects garbage, in place of Python's own trigger

_gc_timer: QTimer | None = None


def _collect_on_gui_thread() -> None:
    """Python's garbage collector runs on whichever thread happens to allocate, so with the worker
    busy it often ran there, and a closed window's widgets freed on the worker crash Qt (a
    segfault in a later QApplication.setStyle, for one). So automatic collection is turned off
    and this timer does it on the GUI thread instead, by the same thresholds Python uses."""
    global _gc_timer
    app = QCoreApplication.instance()
    if _gc_timer is not None or app is None:
        return
    gc.disable()
    _gc_timer = QTimer(app)
    _gc_timer.timeout.connect(_collect)
    _gc_timer.start(GC_MS)


def _collect() -> None:
    counts, limits = gc.get_count(), gc.get_threshold()
    for generation in (2, 1, 0):
        if limits[generation] and counts[generation] > limits[generation]:
            gc.collect(generation)
            return


class _Worker(QObject):
    done = Signal(int, object, object)   # job id, result, exception

    def __init__(self):
        super().__init__()
        self.target: Target | None = None
        self.stopping = False   # set by Device.shutdown: jobs still queued are skipped

    @Slot(int, object)
    def run(self, job_id: int, fn) -> None:
        if self.stopping and job_id != 0:   # job 0 is shutdown's own close (M7)
            return
        try:
            result = fn(self)
        except Exception as e:   # handed to the job's on_error on the GUI thread
            self.done.emit(job_id, None, e)
        else:
            self.done.emit(job_id, result, None)


class Device(QObject):
    opened = Signal(object)          # the Target, once connected
    closed = Signal()
    _run = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread()
        self._thread.setObjectName("rdt-device")
        self._worker = _Worker()
        self._worker.moveToThread(self._thread)
        self._run.connect(self._worker.run)
        self._worker.done.connect(self._finished)
        self._jobs: dict[int, tuple[str | None, Callable | None, Callable | None]] = {}
        self._keys: set[str] = set()
        self._ids = itertools.count(1)
        _collect_on_gui_thread()
        self.target: Target | None = None   # for reading state on the GUI thread only
        # Bumped by every open() and close(): a connect whose number is no longer current was
        # cancelled (F5 again, or another workspace opened), so it closes its own Target and
        # doesn't attach it (H4, M5; rani, 2026-10-18, code review).
        self._link_gen = 0
        self._thread.start()

    # --- jobs --------------------------------------------------------------------------------

    def submit(self, fn: Callable[[Target], Any], on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[Exception], None] | None = None, key: str | None = None,
               needs_target: bool = True) -> int | None:
        """Run ``fn(target)`` on the worker. Returns the job id, or None if dropped (busy key, or no
        link). Without ``needs_target``, ``fn`` gets the worker itself."""
        if key is not None and key in self._keys:
            return None
        if needs_target and self.target is None:
            if on_error:
                on_error(LinkError("not connected"))
            return None

        def job(worker: _Worker):
            if not needs_target:
                return fn(worker)   # the link jobs below manage worker.target themselves
            if worker.target is None:
                raise LinkError("not connected")
            return fn(worker.target)

        job_id = next(self._ids)
        self._jobs[job_id] = (key, on_done, on_error)
        if key is not None:
            self._keys.add(key)
        self._run.emit(job_id, job)
        return job_id

    def busy(self, key: str) -> bool:
        return key in self._keys

    @property
    def pending(self) -> int:
        return len(self._jobs)

    @Slot(int, object, object)
    def _finished(self, job_id: int, result, error) -> None:
        key, on_done, on_error = self._jobs.pop(job_id, (None, None, None))
        if key is not None:
            self._keys.discard(key)
        if error is not None:
            if on_error:
                on_error(error)
            else:
                log.warning("device job failed: %s", error)
        elif on_done:
            on_done(result)

    # --- link --------------------------------------------------------------------------------

    def open(self, config: TargetConfig, on_error: Callable[[Exception], None] | None = None) -> None:
        """Connect to the target described by ``config`` (closing any open link first). A later
        open() or close() cancels it: the Target it made is closed, and neither ``opened`` nor
        ``on_error`` comes."""
        self._link_gen += 1
        gen = self._link_gen

        def connect(worker: _Worker):
            if worker.target is not None:
                worker.target.close()
                worker.target = None
            if gen != self._link_gen:
                return None             # cancelled while it waited its turn
            t = Target(config, auto_reconnect=True)
            try:
                t.connect()
            except Exception:
                t.close()
                raise
            if gen != self._link_gen:   # cancelled while connecting
                t.close()
                return None
            worker.target = t
            return t

        def done(t: Target | None) -> None:
            if t is None or gen != self._link_gen:
                return   # the close() that cancelled it closes this Target too (it runs next)
            self.target = t
            self.opened.emit(t)

        def failed(e: Exception) -> None:
            if gen == self._link_gen and on_error:
                on_error(e)

        self.target = None
        self.submit(connect, done, failed, needs_target=False)

    def close(self, on_done: Callable[[Any], None] | None = None) -> None:
        self._link_gen += 1

        def disconnect(worker: _Worker):
            if worker.target is not None:
                worker.target.close()
                worker.target = None

        def done(_):
            self.closed.emit()
            if on_done:
                on_done(None)

        self.target = None
        self.submit(disconnect, done, needs_target=False)

    @property
    def state(self) -> LinkState:
        return self.target.link_state if self.target is not None else LinkState.CLOSED

    def shutdown(self, wait_ms: int = 5000) -> None:
        """Close the link and stop the worker. Jobs still queued are skipped; the one in progress
        is waited for, up to ``wait_ms``, and then the port is closed, so RDT closes it itself
        (M7). The stop is queued as a job, so the loop ends only after the running job: quit()
        from here would stop it at once. The Target is closed here, on the GUI thread, once the
        worker has ended: Python code run on the worker while windows are being torn down can
        let the garbage collector delete widgets there, which crashes Qt."""
        self._link_gen += 1
        if self._thread.isRunning():
            self._worker.stopping = True
            self._run.emit(0, lambda worker: QThread.currentThread().quit())
            if not self._thread.wait(wait_ms):
                # A job that still hasn't ended (each access has its own timeout, so this is a
                # long job, not a hang). Qt aborts the whole process if a running QThread is
                # destroyed, so the thread and worker are kept until RDT exits, and the port is
                # closed then.
                log.warning("device job still running after %d ms; finishing it as RDT exits", wait_ms)
                _keep_until_exit(self._thread, self._worker)
                self.target = None
                return
        _close_target(self._worker)
        self.target = None


def _close_target(worker: _Worker) -> None:
    if worker.target is not None:
        try:
            worker.target.close()
        except Exception as e:   # closing on the way out: nothing left to tell
            log.warning("closing the link: %s", e)
        worker.target = None


_left_running: list[tuple[QThread, _Worker]] = []


def _keep_until_exit(thread: QThread, worker: _Worker) -> None:
    if not _left_running and not getattr(_keep_until_exit, "registered", False):
        atexit.register(_wait_left_running)
        _keep_until_exit.registered = True
    _left_running.append((thread, worker))


def _wait_left_running() -> None:
    """Runs at exit, before Python tears down the modules (which would destroy the threads)."""
    while _left_running:
        thread, worker = _left_running.pop()
        thread.wait()
        _close_target(worker)


def describe_error(e: Exception) -> str:
    if isinstance(e, ConfigError):
        return str(e)
    return f"{type(e).__name__}: {e}" if not str(e).startswith(type(e).__name__) else str(e)
