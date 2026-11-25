"""Keep a streaming API request interruptible while the socket is idle."""

from contextlib import closing
from queue import Empty, Full, Queue
from socket import SHUT_RDWR
from threading import Event, Thread, local
from time import monotonic


USAGE_GRACE = 1.5
POLL_INTERVAL = 0.1
_current = local()


class StreamIdleTimeout(TimeoutError):
    pass


class _Stream:
    def __init__(self):
        self.cancelled = Event()
        self.queue = Queue(maxsize=32)
        self.socket = None

    def send(self, kind, value=None):
        while not self.cancelled.is_set():
            try:
                self.queue.put((kind, value), timeout=POLL_INTERVAL)
                return True
            except Full:
                continue
        return False

    def interrupt(self):
        self.cancelled.set()
        if self.socket is not None:
            try:
                self.socket.shutdown(SHUT_RDWR)
            except OSError:
                pass


def register_response(response):
    stream = getattr(_current, "stream", None)
    if stream is not None:
        # urllib response wrappers own this socket. Calling response.close()
        # from another thread can wait for a buffered read to release its lock.
        stream.socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if stream.cancelled.is_set():
            stream.interrupt()
    return stream.cancelled.is_set if stream is not None else None


def agent_events(fetch, stopped, idle_timeout):
    stream = _Stream()

    def receive():
        _current.stream = stream
        try:
            with closing(fetch()) as events:
                for event in events:
                    if not stream.send("event", event):
                        return
        except Exception as exc:
            stream.send("error", exc)
        else:
            stream.send("end")
        finally:
            del _current.stream

    if stopped():
        return
    Thread(target=receive, name="codelane-ai-stream", daemon=True).start()
    deadline = monotonic() + idle_timeout
    finish_deadline = None
    try:
        while not stopped():
            remaining = (finish_deadline or deadline) - monotonic()
            if remaining <= 0:
                if finish_deadline is not None:
                    return
                raise StreamIdleTimeout()
            try:
                kind, value = stream.queue.get(timeout=min(POLL_INTERVAL, remaining))
            except Empty:
                continue
            if stopped() or kind == "end":
                return
            if kind == "error":
                if finish_deadline is not None:
                    return  # The answer is complete, only usage may be missing.
                raise value
            if finish_deadline is None and any(
                choice.get("finish_reason") for choice in (value.get("choices") or ())
                if isinstance(choice, dict)
            ):
                finish_deadline = monotonic() + USAGE_GRACE
            deadline = monotonic() + idle_timeout
            yield value
    finally:
        stream.interrupt()
