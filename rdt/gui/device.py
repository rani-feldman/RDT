"""The device link for the GUI: every access runs on one worker thread, in order.

Panels call ``submit(fn, on_done, on_error)``; ``fn(target)`` runs on the worker and the
callbacks run back on the GUI thread. A job with a ``key`` is dropped while an earlier job with
the same key is still waiting, so a poll that can't keep up falls behind instead of queueing.
"""

from __future__ import annotations

import atexit
import itertools
import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..errors import ConfigError, LinkError
from ..session import LinkState
from ..target import Target, TargetConfig

log = logging.getLogger(__name__)


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
        is waited for, up to ``wait_ms``, and then the close runs, so RDT closes the port itself
        (M7). The close job stops the thread's loop only after it ran: quit() from here would
        stop it before the queued close."""
        self._link_gen += 1
        if self._thread.isRunning():
            def close_and_stop(worker: _Worker):
                try:
                    if worker.target is not None:
                        worker.target.close()
                        worker.target = None
                finally:
                    QThread.currentThread().quit()

            self._worker.stopping = True
            self._run.emit(0, close_and_stop)
            if not self._thread.wait(wait_ms):
                # A job that still hasn't ended (each access has its own timeout, so this is a
                # long job, not a hang). Qt aborts the whole process if a running QThread is
                # destroyed, so the thread and worker are kept until RDT exits and waited for
                # then; the close queued above still runs once the job ends.
                log.warning("device job still running after %d ms; finishing it as RDT exits", wait_ms)
                _keep_until_exit(self._thread, self._worker)
        self.target = None


_left_running: list[tuple[QThread, _Worker]] = []


def _keep_until_exit(thread: QThread, worker: _Worker) -> None:
    if not _left_running and not getattr(_keep_until_exit, "registered", False):
        atexit.register(_wait_left_running)
        _keep_until_exit.registered = True
    _left_running.append((thread, worker))


def _wait_left_running() -> None:
    """Runs at exit, before Python tears down the modules (which would destroy the threads)."""
    while _left_running:
        thread, _ = _left_running.pop()
        thread.wait()


def describe_error(e: Exception) -> str:
    if isinstance(e, ConfigError):
        return str(e)
    return f"{type(e).__name__}: {e}" if not str(e).startswith(type(e).__name__) else str(e)
