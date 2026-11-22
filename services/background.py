"""Run short blocking jobs away from the GUI thread.

Qt advances *every* animation from the GUI thread's event loop, so each
millisecond spent there on file-system scans or ``git`` queries is a
millisecond in which the whole workbench visibly freezes. Small one-shot jobs
(file walks, subprocess queries, the first SQLAlchemy import) are therefore
executed by a small pool of long-lived Python worker threads while their result
is delivered back on the GUI thread.

Why Python threads instead of ``QThreadPool``
---------------------------------------------
The jobs are pure Python: they import modules, build SQLAlchemy engines and
shell out to ``git``. A ``QThreadPool`` worker is a C++ thread owned by Qt that
only *borrows* a Python thread state: PyQt calls ``PyGILState_Ensure`` before
the runnable and ``PyGILState_Release`` after it, so the state is created and
destroyed once per job and it is never managed by :mod:`threading` (the thread
does not run ``_bootstrap``). Releasing such a state tears down the frames the
job ran, and with Python 3.15 that teardown hits the interpreter's inline-cache
bookkeeping, which faults inside ``python315.dll`` (Windows Application log:
``python.exe``, exception ``0xc0000005`` at the constant offset ``0x153709`` –
``mov rax,[r9+rcx*8+0x20]`` / ``mov edx,[rax]`` in the code-object cache
refcount loop). The failure takes the whole process down, so it shows up as
``Windows fatal exception: access violation`` in pytest and in the app.

Owning the threads with :mod:`threading` keeps one state per worker for the
lifetime of the process, creates it through the regular ``Thread._bootstrap``
path and destroys it deterministically in :func:`shutdown` (registered with
:mod:`atexit`), long before Qt tears the application down.

Public API
----------
``run_async(fn, on_done=..., on_error=...)``   run *fn* off the GUI thread
``shutdown(wait=True)``                        stop the workers (tests, exit)
"""
from __future__ import annotations

import atexit
import os
import threading
from queue import Queue
from typing import Callable, Optional

from PyQt6 import QtCore

__all__ = ["run_async", "shutdown"]

# Keep the signal objects alive until their callback ran; the caller usually
# does not hold a reference to the returned object.
_ACTIVE: set = set()

_QUEUE: "Queue[Optional[tuple[Callable, object]]]" = Queue()
_LOCK = threading.Lock()
_WORKERS: list[threading.Thread] = []
_IDLE = 0
_CLOSING = False
_MAX_WORKERS = max(2, min(4, (os.cpu_count() or 2) // 2 or 2))


class _TaskSignals(QtCore.QObject):
    """Lives on the GUI thread; the worker only emits through it."""

    finished = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)


def _worker_loop() -> None:
    global _IDLE
    while True:
        with _LOCK:
            _IDLE += 1  # the worker is (about to be) waiting for a job
        job = _QUEUE.get()
        with _LOCK:
            _IDLE -= 1
        if job is None:  # shutdown sentinel
            return
        fn, signals = job
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            try:
                signals.failed.emit(str(exc))
            except RuntimeError:
                # The application may be shutting down already.
                pass
        else:
            try:
                signals.finished.emit(result)
            except RuntimeError:
                pass


def _spawn_worker_locked() -> None:
    worker = threading.Thread(
        target=_worker_loop,
        name=f"pyscribe-bg-{len(_WORKERS) + 1}",
        daemon=False,
    )
    _WORKERS.append(worker)
    worker.start()


def _submit(fn: Callable, signals: _TaskSignals) -> None:
    with _LOCK:
        if _CLOSING:
            return
        if _IDLE == 0 and len(_WORKERS) < _MAX_WORKERS:
            _spawn_worker_locked()
    _QUEUE.put((fn, signals))


def shutdown(wait: bool = True) -> None:
    """Stop the worker threads; called automatically at interpreter exit.

    Tearing the thread states down here (instead of during Qt's own teardown)
    keeps CPython's frame/code-object release on the regular shutdown path.
    """
    global _CLOSING, _IDLE
    with _LOCK:
        if _CLOSING:
            return
        _CLOSING = True
        workers = list(_WORKERS)
        _WORKERS.clear()
    for _worker in workers:
        _QUEUE.put(None)
    if not wait:
        return
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout=10)
    with _LOCK:
        _IDLE = 0


def _stop_workers() -> None:
    """Interpreter-shutdown hook: queue the sentinels, no joining here."""
    shutdown(wait=False)


# ``threading._shutdown`` joins the non-daemon workers before atexit callbacks
# run, so the sentinels must be queued from there - otherwise the process would
# hang on exit with the workers still blocked on the queue.
try:
    threading._register_atexit(_stop_workers)  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover - Python < 3.9
    atexit.register(_stop_workers)


def run_async(fn: Callable, on_done: Optional[Callable] = None,
              on_error: Optional[Callable] = None,
              parent: Optional[QtCore.QObject] = None) -> _TaskSignals:
    """Execute *fn* on a Python worker thread, report on the GUI thread."""
    signals = _TaskSignals(parent)
    _ACTIVE.add(signals)

    def _finish(callback, payload):
        _ACTIVE.discard(signals)
        try:
            if callback is not None:
                callback(payload)
        except RuntimeError:
            pass
        finally:
            signals.deleteLater()

    signals.finished.connect(lambda result: _finish(on_done, result))
    signals.failed.connect(lambda message: _finish(on_error, message))
    _submit(fn, signals)
    return signals
