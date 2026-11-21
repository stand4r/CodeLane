"""Process lifetime ownership for the integrated terminal."""

import os
from pathlib import Path
import shutil
import signal

from PyQt6 import QtCore


def _windows_job(pid):
    import ctypes as c
    from ctypes import wintypes as w

    class Limits(c.Structure):
        _fields_ = [
            ("process_time", c.c_longlong), ("job_time", c.c_longlong),
            ("flags", w.DWORD), ("minimum_ws", c.c_size_t), ("maximum_ws", c.c_size_t),
            ("active_processes", w.DWORD), ("affinity", c.c_size_t),
            ("priority", w.DWORD), ("scheduling", w.DWORD),
        ]

    class ExtendedLimits(c.Structure):
        _fields_ = [
            ("basic", Limits), ("io", c.c_ulonglong * 6), ("process_memory", c.c_size_t),
            ("job_memory", c.c_size_t), ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t),
        ]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (c.c_void_p, w.LPCWSTR)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = (w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    job = kernel.CreateJobObjectW(None, None)
    process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
    try:
        if not job or not process:
            return None
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            return None
        if not kernel.AssignProcessToJobObject(job, process):
            return None
        handle, job = job, None
        return lambda: kernel.CloseHandle(handle)
    finally:
        if process:
            kernel.CloseHandle(process)
        if job:
            kernel.CloseHandle(job)


class TerminalProcess(QtCore.QProcess):
    """Group shell and program descendants so closing a tab can stop them."""

    def __init__(self, parent):
        super().__init__(parent)
        self._close_job = None
        self._group = 0
        self._setsid = None if os.name == "nt" else shutil.which("setsid")
        self._kill_pending = False
        self.started.connect(self._attach_tree)
        self.finished.connect(self._release_tree)

    def start(self, program, arguments):
        self._kill_pending = False
        if self._setsid:
            super().start(self._setsid, ["--", program, *arguments])
        else:
            super().start(program, arguments)

    def _attach_tree(self):
        pid = int(self.processId())
        if os.name == "nt":
            self._close_job = _windows_job(pid)
        elif self._setsid:
            self._group = pid
        if self._kill_pending:
            self.terminate_tree()

    def _release_tree(self):
        if self._close_job is not None:
            close, self._close_job = self._close_job, None
            close()
        if self._group:
            group, self._group = self._group, 0
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass

    def terminate_tree(self):
        if self.state() == self.ProcessState.Starting:
            self._kill_pending = True
            return
        if self._close_job is not None or self._group:
            self._release_tree()
            self.kill()
            return
        pid = int(self.processId())
        if os.name == "nt" and pid:
            killer = QtCore.QProcess(QtCore.QCoreApplication.instance())
            if hasattr(killer, "setCreateProcessArgumentsModifier"):
                killer.setCreateProcessArgumentsModifier(
                    lambda args: setattr(args, "flags", args.flags | 0x08000000)
                )

            def done(*_args):
                try:
                    if self.processId() == pid:
                        self.kill()
                except RuntimeError:
                    pass
                killer.deleteLater()

            killer.finished.connect(done)
            killer.errorOccurred.connect(done)
            target = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            killer.start(str(target), ["/PID", str(pid), "/T", "/F"])
        else:
            self.kill()
