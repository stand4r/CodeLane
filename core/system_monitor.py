"""Живые метрики машины и мониторинг процессов для ИИ-агента.

Модуль отвечает на запрос «что происходит на ПК прямо сейчас»: загрузка CPU,
память, топ процессов, появление/завершение процессов во время серии действий
агента. Использует уже установленный ``psutil``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

try:  # pragma: no cover - psutil обязателен для CodeLane
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

__all__ = ["MonitorError", "find_processes", "system_snapshot", "watch_processes"]

MAX_WATCH_SECONDS = 30.0
MAX_INTERVAL = 5.0


class MonitorError(RuntimeError):
    """Мониторинг недоступен."""


def _require_psutil():
    if psutil is None:
        raise MonitorError("psutil недоступен — мониторинг процессов невозможен.")
    return psutil


def _prime_cpu(timeout: float = 0.25) -> None:
    """Первый cpu_percent() у psutil всегда 0.0 — прогреваем счётчики."""
    mod = _require_psutil()
    mod.cpu_percent(interval=None)
    for proc in mod.process_iter(["pid"]):
        try:
            proc.cpu_percent(interval=None)
        except (mod.NoSuchProcess, mod.AccessDenied):
            continue
    time.sleep(max(0.0, min(2.0, timeout)))


def _proc_row(proc, with_cmdline: bool = False) -> Optional[Dict[str, Any]]:
    mod = _require_psutil()
    try:
        info = proc.info
        memory = info.get("memory_info")
        rss = int(getattr(memory, "rss", 0) or 0) / (1024 * 1024)
        row = {
            "pid": int(info.get("pid") or 0),
            "name": info.get("name") or "",
            "cpu": round(float(info.get("cpu_percent") or 0.0), 1),
            "ram_mb": round(rss, 1),
            "status": info.get("status") or "",
            "exe": info.get("exe") or "",
        }
        if with_cmdline:
            try:
                row["cmdline"] = " ".join(proc.cmdline())[:300]
            except (mod.AccessDenied, mod.NoSuchProcess):
                row["cmdline"] = ""
        return row
    except (mod.NoSuchProcess, mod.AccessDenied, mod.ZombieProcess):
        return None


def _collect(pattern: Optional[str] = None, top: int = 10,
             with_cmdline: bool = False) -> List[Dict[str, Any]]:
    mod = _require_psutil()
    attributes = ["pid", "name", "cpu_percent", "memory_info", "status", "exe"]
    rows: List[Dict[str, Any]] = []
    regex = None
    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise MonitorError(f"Некорректное регулярное выражение: {exc}") from exc
    for proc in mod.process_iter(attributes):
        row = _proc_row(proc, with_cmdline=with_cmdline)
        if not row:
            continue
        if regex is not None and not (regex.search(row["name"])
                                      or regex.search(row["exe"])
                                      or regex.search(row.get("cmdline", ""))):
            continue
        rows.append(row)
    rows.sort(key=lambda item: (item["cpu"], item["ram_mb"]), reverse=True)
    return rows[:max(1, min(50, int(top)))]


def _memory_snapshot() -> Dict[str, Any]:
    mod = _require_psutil()
    mem = mod.virtual_memory()
    return {
        "total_mb": round(mem.total / (1024 * 1024)),
        "used_mb": round((mem.total - mem.available) / (1024 * 1024)),
        "percent": float(mem.percent),
    }


def system_snapshot(top: int = 8, with_windows: bool = True) -> Dict[str, Any]:
    """Один кадр состояния машины: CPU, RAM, топ процессов, активное окно."""
    _prime_cpu()
    data: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 1),
        "cpu_cores": int(psutil.cpu_count(logical=True) or 0),
        "memory": _memory_snapshot(),
        "process_count": len(psutil.pids()),
        "top_processes": _collect(top=top),
    }
    try:
        data["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    except Exception:  # noqa: BLE001 - не критично для отчёта
        pass
    if with_windows:
        try:
            from core.desktop_control import foreground_window
            window = foreground_window()
            data["foreground_window"] = {
                "title": window["title"], "pid": window["pid"],
                "rect": window["rect"],
            }
        except Exception:  # noqa: BLE001 - вне Windows просто нет окна
            pass
    return data


def find_processes(pattern: Optional[str] = None, top: int = 10,
                   with_cmdline: bool = True) -> Dict[str, Any]:
    """Найти процессы по имени/exe/cmdline (регулярное выражение, регистр не важен)."""
    if pattern and len(str(pattern)) > 200:
        raise MonitorError("Слишком длинный шаблон поиска.")
    _prime_cpu()
    rows = _collect(pattern=pattern, top=top, with_cmdline=with_cmdline)
    return {"pattern": pattern or "", "count": len(rows), "processes": rows}


def watch_processes(duration_sec: float = 3.0, interval_sec: float = 1.0,
                    top: int = 5, pattern: Optional[str] = None) -> Dict[str, Any]:
    """Снимать метрики несколько секунд подряд и показать старт/остановку процессов.

    Возвращает список кадров и события ``started``/``stopped`` — именно это
    нужно, чтобы «в реальном времени» проверить, что запустилось после
    действий агента.
    """
    mod = _require_psutil()
    try:
        duration = max(0.5, min(MAX_WATCH_SECONDS, float(duration_sec)))
        interval = max(0.2, min(MAX_INTERVAL, float(interval_sec)))
    except (TypeError, ValueError) as exc:
        raise MonitorError("duration_sec/interval_sec должны быть числами.") from exc
    if interval > duration:
        interval = duration

    _prime_cpu()
    baseline: Dict[int, str] = {}
    for proc in mod.process_iter(["pid", "name"]):
        try:
            baseline[int(proc.info["pid"])] = proc.info.get("name") or ""
        except (mod.NoSuchProcess, mod.AccessDenied):
            continue

    samples: List[Dict[str, Any]] = []
    started: Dict[int, str] = {}
    stopped: Dict[int, str] = {}
    deadline = time.monotonic() + duration
    while True:
        cpu = round(float(psutil.cpu_percent(interval=None)), 1)
        memory = _memory_snapshot()
        rows = _collect(pattern=pattern, top=top)
        alive: Dict[int, str] = {}
        for proc in mod.process_iter(["pid", "name"]):
            try:
                alive[int(proc.info["pid"])] = proc.info.get("name") or ""
            except (mod.NoSuchProcess, mod.AccessDenied):
                continue
        for pid, name in alive.items():
            if pid not in baseline:
                started.setdefault(pid, name)
        for pid, name in baseline.items():
            if pid not in alive:
                stopped.setdefault(pid, name)
        samples.append({
            "at": round(duration - (deadline - time.monotonic()), 2),
            "cpu_percent": cpu,
            "memory_percent": memory["percent"],
            "process_count": len(alive),
            "top": [{"name": row["name"], "cpu": row["cpu"], "ram_mb": row["ram_mb"]}
                    for row in rows[:3]],
        })
        remaining = deadline - time.monotonic()
        if remaining <= 0.01:
            break
        time.sleep(min(interval, remaining))

    def _names(items: Dict[int, str]) -> List[Dict[str, Any]]:
        return [{"pid": pid, "name": name} for pid, name in list(items.items())[:20]]

    return {
        "duration_sec": round(duration, 2),
        "interval_sec": round(interval, 2),
        "samples": samples,
        "started": _names(started),
        "stopped": _names(stopped),
    }
