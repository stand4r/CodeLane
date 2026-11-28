"""Bounded, cancellable command sessions with incremental file-backed output."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from functools import wraps
from threading import Event, RLock, Thread
import time
from uuid import uuid4

import psutil

from core.subprocess_utils import decode_console_output, popen_hidden


class CommandSession:
    def __init__(self, launch, *, shell, cwd, env, timeout, should_stop, command):
        self.id = uuid4().hex
        self.command = command
        self.cwd = str(cwd)
        self.offset = 0
        self.reason = ""
        self.lock = RLock()
        self.finished = Event()
        self.capture = tempfile.NamedTemporaryFile(prefix="codelane-command-", delete=False)
        self.path = Path(self.capture.name)
        try:
            self.process = popen_hidden(launch, shell=shell, cwd=str(cwd), env=env,
                                        stdin=subprocess.PIPE, stdout=self.capture, stderr=subprocess.STDOUT,
                                        **({"start_new_session": True} if os.name != "nt" else {}))
        except Exception:
            self.capture.close()
            self.path.unlink(missing_ok=True)
            raise
        self.monitor = Thread(target=self._monitor, args=(timeout, should_stop), daemon=True)
        self.monitor.start()

    def _monitor(self, timeout, should_stop):
        deadline = time.monotonic() + timeout
        try:
            while self.process.poll() is None:
                if should_stop and should_stop():
                    self.stop("cancelled")
                    break
                if time.monotonic() >= deadline:
                    self.stop("timeout")
                    break
                if self.path.stat().st_size > 32 * 1024 * 1024:
                    self.stop("output_limit")
                    break
                time.sleep(0.05)
        finally:
            self.finished.set()

    def stop(self, reason="terminated"):
        with self.lock:
            if self.process.poll() is not None:
                return
            self.reason = reason
            try:
                if os.name == "nt":
                    # taskkill is another process and can fail to launch during
                    # system-wide process startup faults. psutil is already a
                    # runtime dependency and stops the tree in this process.
                    parent = psutil.Process(self.process.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        parent.terminate()
                    except psutil.NoSuchProcess:
                        pass
                    _, alive = psutil.wait_procs([*children, parent], timeout=2)
                    for process in alive:
                        try:
                            process.kill()
                        except psutil.NoSuchProcess:
                            pass
                    if alive:
                        psutil.wait_procs(alive, timeout=2)
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError, psutil.Error):
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=2)

    def poll(self, *, chars="", yield_time_ms=1000, max_output_chars=12000, terminate=False):
        if not isinstance(chars, str) or len(chars.encode("utf-8")) > 4000:
            raise ValueError("chars должен быть строкой до 4000 байт UTF-8.")
        if type(yield_time_ms) is not int or not 0 <= yield_time_ms <= 10000:
            raise ValueError("yield_time_ms должен быть от 0 до 10000.")
        if type(max_output_chars) is not int or not 256 <= max_output_chars <= 48000:
            raise ValueError("max_output_chars должен быть от 256 до 48000.")
        if terminate:
            self.stop()
        elif chars:
            if self.process.poll() is not None:
                raise ValueError("Процесс уже завершён.")
            error = []

            def send():
                try:
                    self.process.stdin.write(chars.encode("utf-8"))
                    self.process.stdin.flush()
                except OSError as exc:
                    error.append(exc)

            writer = Thread(target=send, daemon=True)
            writer.start()
            writer.join(2)
            if writer.is_alive():
                self.stop("stdin_timeout")
                raise ValueError("Процесс не принимает ввод и был остановлен.")
            if error:
                raise ValueError(f"Не удалось передать ввод: {error[0]}")
        self.finished.wait(yield_time_ms / 1000)
        with self.lock, self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(max_output_chars)
            # Do not split a UTF-8 codepoint between incremental pages.
            if data:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    if exc.reason == "unexpected end of data" and self.process.poll() is None:
                        data = data[:exc.start]
            self.offset += len(data)
            more = self.path.stat().st_size > self.offset
            code = self.process.poll()
            return {"session_id": self.id, "command": self.command, "cwd": self.cwd,
                    "output": decode_console_output(data), "exit_code": code,
                    "completed": code is not None, "has_more_output": more,
                    "reason": self.reason}

    def close(self):
        self.stop()
        self.monitor.join(3)
        if self.process.stdin:
            self.process.stdin.close()
        self.capture.close()
        self.path.unlink(missing_ok=True)


class CommandSessions:
    def __init__(self):
        self.sessions = {}
        self.lock = RLock()

    def start(self, launch, **options):
        with self.lock:
            if len(self.sessions) >= 8:
                raise ValueError("Не более 8 командных сессий. Дочитай вывод завершённых команд.")
            session = CommandSession(launch, **options)
            self.sessions[session.id] = session
            return session.id

    def poll(self, session_id, **options):
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise ValueError("Командная сессия не найдена или уже закрыта.")
            result = session.poll(**options)
            if result["completed"] and not result["has_more_output"]:
                self.sessions.pop(session_id)
                session.close()
            return result

    def close(self):
        with self.lock:
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()


def close_task_commands(function):
    """Top-level tasks own sessions; child agents may share them until the task ends."""
    @wraps(function)
    def run(service, toolbox, *args, **kwargs):
        try:
            return function(service, toolbox, *args, **kwargs)
        finally:
            close = getattr(toolbox, "close", None)
            if close:
                close()
    return run
