"""The device link for the GUI: every access runs on one worker thread, in order.

Panels call ``submit(fn, on_done, on_error)``; ``fn(target)`` runs on the worker and the
callbacks run back on the GUI thread. A job with a ``key`` is dropped while an earlier job with
the same key is still waiting, so a poll that can't keep up falls behind instead of queueing.
"""

from __future__ import annotations

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

    @Slot(int, object)
    def run(self, job_id: int, fn) -> None:
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
        """Connect to the target described by ``config`` (closing any open link first)."""

        def connect(worker: _Worker):
            if worker.target is not None:
                worker.target.close()
                worker.target = None
            t = Target(config, auto_reconnect=True)
            try:
                t.connect()
            except Exception:
                t.close()
                raise
            worker.target = t
            return t

        def done(t: Target) -> None:
            self.target = t
            self.opened.emit(t)

        self.target = None
        self.submit(connect, done, on_error, needs_target=False)

    def close(self, on_done: Callable[[Any], None] | None = None) -> None:
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

    def shutdown(self) -> None:
        """Close the link and stop the worker; waits for the job in progress."""
        if self._thread.isRunning():
            self._run.emit(0, lambda w: w.target.close() if w.target else None)
            self._thread.quit()
            self._thread.wait(5000)
        self.target = None


def describe_error(e: Exception) -> str:
    if isinstance(e, ConfigError):
        return str(e)
    return f"{type(e).__name__}: {e}" if not str(e).startswith(type(e).__name__) else str(e)
