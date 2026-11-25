"""Живое управление рабочим столом Windows без сторонних зависимостей.

Модуль даёт ИИ-агенту то, чего не хватало для работы «как Claude Code»:
реальный ввод мыши и клавиатуры (Win32 ``SendInput``), список/фокус окон и
снимки экрана в PNG через GDI (``BitBlt``/``StretchBlt``).

Особенности:

* импорт безопасен на Linux/macOS — все Win32-вызовы ленивые;
* никакого ``pyautogui``: только ``ctypes``, поэтому бинарник CodeLane не
  получает новых зависимостей;
* снимок умеет масштабироваться средствами GDI (``HALFTONE``), чтобы кадр
  не занимал сотни тысяч токенов у модели;
* все координаты проверяются по виртуальному экрану (мультимониторность).

Публичный API: :func:`capabilities`, :func:`capture_png`,
:func:`mouse_move`, :func:`mouse_click`, :func:`mouse_down`, :func:`mouse_up`,
:func:`mouse_scroll`, :func:`mouse_drag`, :func:`type_text`, :func:`press_keys`,
:func:`list_windows`, :func:`foreground_window`, :func:`focus_window`,
:func:`cursor_position`, :func:`screen_rect`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import struct
import sys
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DesktopControlError",
    "capabilities",
    "capture_png",
    "cursor_position",
    "focus_window",
    "foreground_window",
    "list_windows",
    "mouse_click",
    "mouse_drag",
    "mouse_down",
    "mouse_move",
    "mouse_scroll",
    "mouse_up",
    "press_keys",
    "save_screenshot",
    "screen_rect",
    "type_text",
    "watch_screen",
]

IS_WINDOWS = os.name == "nt"

MAX_PNG_WIDTH = 1600
MIN_PNG_WIDTH = 320

# SendInput flags
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x01000
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0
_HALFTONE = 4
_SW_RESTORE = 9
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CMONITORS = 80

_BUTTON_FLAGS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}

_KEY_NAMES = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pri": 0x21,
    "pagedown": 0x22,
    "next": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "menu": 0x5D,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "pause": 0x13,
}

_ALIASES = {
    "control": "ctrl",
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "prior": "pageup",
    "next": "pagedown",
}


class DesktopControlError(RuntimeError):
    """Управление рабочим столом невозможно или действие отклонено."""


# --------------------------------------------------------------------------
# ленивая инициализация Win32
# --------------------------------------------------------------------------
_user32 = None
_gdi32 = None


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise DesktopControlError(
            "Живое управление рабочим столом реализовано для Windows.")


def _libs():
    global _user32, _gdi32
    _require_windows()
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        _declare_prototypes(_user32, _gdi32)
    return _user32, _gdi32


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _declare_prototypes(user32, gdi32) -> None:
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int


_DPI_READY = False


def _ensure_dpi_aware() -> None:
    """Без DPI-awareness координаты и снимки «съезжают» на масштабе 125–200 %."""
    global _DPI_READY
    if _DPI_READY:
        return
    _DPI_READY = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - Qt обычно уже выставил awareness
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# геометрия
# --------------------------------------------------------------------------
def screen_rect() -> Dict[str, int]:
    """Виртуальный экран: объединение всех мониторов."""
    user32, _ = _libs()
    _ensure_dpi_aware()
    left = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    monitors = max(1, user32.GetSystemMetrics(_SM_CMONITORS))
    return {"left": left, "top": top, "width": max(1, width),
            "height": max(1, height), "monitors": monitors}


def cursor_position() -> Tuple[int, int]:
    user32, _ = _libs()
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise DesktopControlError("Не удалось прочитать позицию курсора.")
    return int(point.x), int(point.y)


def _clamp_point(x: Any, y: Any) -> Tuple[int, int]:
    rect = screen_rect()
    try:
        px, py = int(round(float(x))), int(round(float(y)))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(f"Некорректные координаты: {x!r}, {y!r}") from exc
    if not (rect["left"] <= px < rect["left"] + rect["width"]):
        raise DesktopControlError(
            f"X={px} вне экрана ({rect['left']}…{rect['left'] + rect['width'] - 1}).")
    if not (rect["top"] <= py < rect["top"] + rect["height"]):
        raise DesktopControlError(
            f"Y={py} вне экрана ({rect['top']}…{rect['top'] + rect['height'] - 1}).")
    return px, py


# --------------------------------------------------------------------------
# ввод
# --------------------------------------------------------------------------
def _send(inputs: Sequence[_INPUT]) -> None:
    user32, _ = _libs()
    array = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise DesktopControlError(
            f"SendInput принял {sent} из {len(inputs)} событий "
            "(возможно, окно защищено или UIPI блокирует ввод).")


def _mouse_input(flags: int, data: int = 0) -> _INPUT:
    event = _INPUT()
    event.type = 0  # INPUT_MOUSE
    event.mi = _MOUSEINPUT(0, 0, data, flags, 0, None)
    return event


def _key_input(vk: int, flags: int = 0, scan: int = 0) -> _INPUT:
    event = _INPUT()
    event.type = 1  # INPUT_KEYBOARD
    event.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    return event


def mouse_move(x: Any, y: Any) -> Dict[str, Any]:
    user32, _ = _libs()
    px, py = _clamp_point(x, y)
    if not user32.SetCursorPos(px, py):
        raise DesktopControlError("SetCursorPos отклонён системой.")
    return {"moved_to": [px, py]}


def mouse_down(button: str = "left") -> Dict[str, Any]:
    flags = _BUTTON_FLAGS.get(str(button).lower())
    if flags is None:
        raise DesktopControlError(f"Неизвестная кнопка мыши: {button!r}")
    _send([_mouse_input(flags[0])])
    return {"button_down": str(button).lower()}


def mouse_up(button: str = "left") -> Dict[str, Any]:
    flags = _BUTTON_FLAGS.get(str(button).lower())
    if flags is None:
        raise DesktopControlError(f"Неизвестная кнопка мыши: {button!r}")
    _send([_mouse_input(flags[1])])
    return {"button_up": str(button).lower()}


def mouse_click(button: str = "left", count: int = 1,
                x: Any = None, y: Any = None, delay: float = 0.05) -> Dict[str, Any]:
    name = str(button or "left").lower()
    flags = _BUTTON_FLAGS.get(name)
    if flags is None:
        raise DesktopControlError(f"Неизвестная кнопка мыши: {button!r}")
    try:
        clicks = max(1, min(3, int(count)))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError("count должен быть числом 1–3.") from exc
    moved = None
    if x is not None and y is not None:
        moved = mouse_move(x, y)
    events: List[_INPUT] = []
    for _ in range(clicks):
        events.append(_mouse_input(flags[0]))
        events.append(_mouse_input(flags[1]))
    _send(events)
    time.sleep(max(0.0, min(1.0, float(delay or 0.0))))
    result: Dict[str, Any] = {"click": name, "count": clicks}
    if moved:
        result.update(moved)
    return result


def mouse_scroll(amount: Any, horizontal: bool = False) -> Dict[str, Any]:
    try:
        notches = int(amount)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError("amount для scroll должен быть целым числом.") from exc
    notches = max(-50, min(50, notches))
    if notches == 0:
        raise DesktopControlError("amount=0 ничего не прокручивает.")
    flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
    # положительное значение — вверх/вправо, как у колеса Windows
    _send([_mouse_input(flag, ctypes.c_ulong(notches * 120).value)])
    return {"scrolled": notches, "horizontal": bool(horizontal)}


def mouse_drag(x1: Any, y1: Any, x2: Any, y2: Any,
               button: str = "left", duration: float = 0.35,
               steps: int = 12) -> Dict[str, Any]:
    name = str(button or "left").lower()
    flags = _BUTTON_FLAGS.get(name)
    if flags is None:
        raise DesktopControlError(f"Неизвестная кнопка мыши: {button!r}")
    start = _clamp_point(x1, y1)
    end = _clamp_point(x2, y2)
    try:
        total = max(2, min(60, int(steps)))
        span = max(0.05, min(5.0, float(duration)))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError("Некорректные duration/steps.") from exc
    mouse_move(*start)
    time.sleep(0.05)
    _send([_mouse_input(flags[0])])
    try:
        for index in range(1, total + 1):
            ratio = index / total
            mouse_move(start[0] + (end[0] - start[0]) * ratio,
                       start[1] + (end[1] - start[1]) * ratio)
            time.sleep(span / total)
    finally:
        _send([_mouse_input(flags[1])])
    return {"dragged": [list(start), list(end)], "button": name}


def _normalize_key(name: str) -> str:
    key = str(name or "").strip().lower()
    return _ALIASES.get(key, key)


def _vk_for(key: str) -> int:
    name = _normalize_key(key)
    if name in _KEY_NAMES:
        return _KEY_NAMES[name]
    if len(name) == 1:
        user32, _ = _libs()
        code = user32.VkKeyScanW(name)
        if code == -1:
            raise DesktopControlError(f"Не удалось преобразовать клавишу {key!r}.")
        return int(code) & 0xFF
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    raise DesktopControlError(f"Неизвестная клавиша: {key!r}")


def press_keys(keys: Any) -> Dict[str, Any]:
    """Нажать комбинацию: ``"ctrl+shift+s"`` или список ``["enter"]``."""
    if isinstance(keys, str):
        parts = [item for item in keys.replace(" ", "").split("+") if item] or [keys]
    elif isinstance(keys, Iterable):
        parts = [str(item) for item in keys]
    else:
        raise DesktopControlError("keys должен быть строкой 'ctrl+s' или списком.")
    if not parts:
        raise DesktopControlError("Пустая комбинация клавиш.")
    codes = [_vk_for(part) for part in parts]
    events: List[_INPUT] = [_key_input(code) for code in codes]
    events.extend(_key_input(code, _KEYEVENTF_KEYUP) for code in reversed(codes))
    _send(events)
    time.sleep(0.02)
    return {"keys": parts}


def type_text(text: Any, interval: float = 0.0) -> Dict[str, Any]:
    """Напечатать юникод-текст в активное окно."""
    value = str(text if text is not None else "")
    if not value:
        raise DesktopControlError("Пустой текст для ввода.")
    if len(value) > 4000:
        raise DesktopControlError("Слишком длинный текст (лимит 4000 символов).")
    try:
        pause = max(0.0, min(0.5, float(interval or 0.0)))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError("interval должен быть числом.") from exc
    units = value.encode("utf-16-le")
    typed = 0
    index = 0
    while index < len(units):
        code = units[index] | (units[index + 1] << 8)
        index += 2
        if code == 13 or (code == 10 and (index >= len(units))):
            _send([_key_input(0x0D), _key_input(0x0D, _KEYEVENTF_KEYUP)])
            typed += 1
            continue
        if code == 10:
            continue  # часть CRLF
        if code == 9:
            _send([_key_input(0x09), _key_input(0x09, _KEYEVENTF_KEYUP)])
            typed += 1
            continue
        _send([
            _key_input(0, _KEYEVENTF_UNICODE, code),
            _key_input(0, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, code),
        ])
        typed += 1
        if pause:
            time.sleep(pause)
    return {"typed_chars": typed}


# --------------------------------------------------------------------------
# окна
# --------------------------------------------------------------------------
def _window_info(hwnd: int, foreground: Optional[int] = None) -> Dict[str, Any]:
    user32, _ = _libs()
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buffer, length + 2)
    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "title": buffer.value,
        "class": class_buffer.value,
        "pid": int(pid.value),
        "rect": [int(rect.left), int(rect.top),
                 int(rect.right - rect.left), int(rect.bottom - rect.top)],
        "minimized": bool(user32.IsIconic(hwnd)),
        "foreground": int(hwnd) == int(foreground or 0),
    }


def foreground_window() -> Dict[str, Any]:
    user32, _ = _libs()
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise DesktopControlError("Активное окно не определено.")
    return _window_info(hwnd, hwnd)


def list_windows(include_empty: bool = False, limit: int = 40) -> List[Dict[str, Any]]:
    user32, _ = _libs()
    foreground = int(user32.GetForegroundWindow() or 0)
    found: List[Dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        info = _window_info(hwnd, foreground)
        if info["title"] or include_empty:
            found.append(info)
        return True

    callback = callback_type(_callback)
    user32.EnumWindows(callback, 0)
    found.sort(key=lambda item: (not item["foreground"], item["title"].lower()))
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 40
    return found[:limit]


def focus_window(title: Optional[str] = None, hwnd: Optional[int] = None,
                 exact: bool = False) -> Dict[str, Any]:
    user32, _ = _libs()
    target = None
    if hwnd:
        target = int(hwnd)
    elif title:
        needle = str(title).strip().lower()
        candidates = list_windows(include_empty=False, limit=200)
        for item in candidates:
            haystack = item["title"].lower()
            if (haystack == needle) if exact else (needle in haystack):
                target = item["hwnd"]
                break
    if not target:
        raise DesktopControlError(f"Окно не найдено: {title or hwnd!r}")
    if user32.IsIconic(target):
        user32.ShowWindow(target, _SW_RESTORE)
        time.sleep(0.15)
    user32.BringWindowToTop(target)
    ok = user32.SetForegroundWindow(target)
    time.sleep(0.05)
    if not ok and int(user32.GetForegroundWindow() or 0) != target:
        raise DesktopControlError(
            "Windows не разрешила перевести окно на передний план "
            "(попробуйте кликнуть по нему вручную).")
    return _window_info(target, target)


# --------------------------------------------------------------------------
# снимки экрана
# --------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(width: int, height: int, rows: Sequence[bytes]) -> bytes:
    """Собрать PNG (RGB, 8 бит) из готовых строк пикселей."""
    if len(rows) != height:
        raise DesktopControlError("Внутренняя ошибка: неверное число строк PNG.")
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(raw, 6))
            + _png_chunk(b"IEND", b""))


def capture_png(region: Optional[Dict[str, Any]] = None,
                max_width: Optional[int] = MAX_PNG_WIDTH,
                grayscale: bool = False) -> Dict[str, Any]:
    """Снимок экрана в PNG.

    ``region`` — ``{"x", "y", "width", "height"}`` в координатах экрана;
    без региона снимается весь виртуальный экран. ``max_width`` ограничивает
    ширину кадра (масштаб делает GDI, чтобы не терять качество текста).
    """
    user32, gdi32 = _libs()
    _ensure_dpi_aware()
    full = screen_rect()
    if region:
        try:
            left = int(region.get("x", region.get("left", full["left"])))
            top = int(region.get("y", region.get("top", full["top"])))
            width = int(region.get("width", full["width"]))
            height = int(region.get("height", full["height"]))
        except (TypeError, ValueError) as exc:
            raise DesktopControlError("Некорректный region снимка.") from exc
        if width <= 0 or height <= 0:
            raise DesktopControlError("Ширина и высота снимка должны быть > 0.")
        left = max(full["left"], min(left, full["left"] + full["width"] - 1))
        top = max(full["top"], min(top, full["top"] + full["height"] - 1))
        width = min(width, full["left"] + full["width"] - left)
        height = min(height, full["top"] + full["height"] - top)
    else:
        left, top, width, height = (full["left"], full["top"],
                                    full["width"], full["height"])

    limit = 0
    if max_width:
        try:
            limit = max(0, int(max_width))
        except (TypeError, ValueError):
            limit = MAX_PNG_WIDTH
    target_w, target_h = width, height
    if limit and limit < width:
        target_w = max(64, limit)
        target_h = max(1, int(round(height * target_w / width)))

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise DesktopControlError("GetDC вернул пустой контекст экрана.")
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = None
    old = None
    try:
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = target_w
        info.bmiHeader.biHeight = -target_h  # сверху вниз
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info),
                                        _DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            raise DesktopControlError("Не удалось создать DIB-секцию для снимка.")
        old = gdi32.SelectObject(mem_dc, bitmap)
        if target_w == width and target_h == height:
            ok = gdi32.BitBlt(mem_dc, 0, 0, width, height,
                              screen_dc, left, top, _SRCCOPY | _CAPTUREBLT)
        else:
            gdi32.SetStretchBltMode(mem_dc, _HALFTONE)
            ok = gdi32.StretchBlt(mem_dc, 0, 0, target_w, target_h,
                                  screen_dc, left, top, width, height,
                                  _SRCCOPY | _CAPTUREBLT)
        if not ok:
            raise DesktopControlError("BitBlt/StretchBlt завершились ошибкой.")
        stride = target_w * 4
        raw = ctypes.string_at(bits, stride * target_h)
    finally:
        if old:
            gdi32.SelectObject(mem_dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    rows: List[bytes] = []
    view = memoryview(raw)
    for y in range(target_h):
        row = view[y * stride:(y + 1) * stride]
        blue = row[0::4]
        green = row[1::4]
        red = row[2::4]
        if grayscale:
            grey = bytearray(target_w)
            for index in range(target_w):
                grey[index] = (int(red[index]) * 299 + int(green[index]) * 587
                               + int(blue[index]) * 114) // 1000
            rows.append(bytes(grey) * 3)
        else:
            out = bytearray(target_w * 3)
            out[0::3] = red
            out[1::3] = green
            out[2::3] = blue
            rows.append(bytes(out))

    data = encode_png(target_w, target_h, rows)
    return {
        "png": data,
        "width": target_w,
        "height": target_h,
        "source": {"x": left, "y": top, "width": width, "height": height},
        "scale": round(target_w / width, 3) if width else 1.0,
    }


def capabilities() -> Dict[str, Any]:
    """Краткое описание доступных возможностей (для отчёта агенту)."""
    if not IS_WINDOWS:
        return {"available": False, "reason": "только Windows"}
    try:
        rect = screen_rect()
        window = foreground_window()
    except DesktopControlError as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "screen": rect,
        "foreground": {"title": window["title"], "pid": window["pid"]},
        "python": sys.version.split()[0],
        "cursor": list(cursor_position()),
    }


def save_screenshot(path: Optional[str] = None,
                    region: Optional[Dict[str, Any]] = None,
                    max_width: Optional[int] = MAX_PNG_WIDTH) -> Dict[str, Any]:
    """Сделать снимок экрана и сохранить его в PNG на диск.

    Без ``path`` файл попадает в ``~/CodeLane/screenshots`` (путь можно
    переопределить переменной окружения ``CODELANE_SCREENSHOT_DIR``).
    """
    frame = capture_png(region=region, max_width=max_width)
    if path:
        target = os.path.abspath(str(path))
    else:
        base = (os.environ.get("CODELANE_SCREENSHOT_DIR")
                or os.path.join(os.path.expanduser("~"), "CodeLane", "screenshots"))
        os.makedirs(base, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(base, f"screen-{stamp}-{int(time.time() * 1000) % 1000:03d}.png")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(frame["png"])
    return {
        "path": target,
        "width": frame["width"],
        "height": frame["height"],
        "size_bytes": len(frame["png"]),
        "taken_at": round(time.time(), 3),
    }


def watch_screen(duration_sec: float = 3.0,
                 interval_sec: float = 0.5,
                 max_width: Optional[int] = MAX_PNG_WIDTH,
                 save_dir: Optional[str] = None) -> Dict[str, Any]:
    """Серия кадров экрана в реальном времени.

    Возвращает метаданные кадров с метками времени и координатами курсора,
    а также последний кадр в ``last_png`` (PNG-байты). Если задан
    ``save_dir``, каждый кадр дополнительно сохраняется на диск.
    """
    duration = max(0.1, float(duration_sec))
    interval = max(0.05, float(interval_sec))
    frames: List[Dict[str, Any]] = []
    last_png: Optional[bytes] = None
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
    deadline = time.monotonic() + duration
    index = 0
    while True:
        frame = capture_png(max_width=max_width)
        item: Dict[str, Any] = {
            "index": index,
            "t": round(time.monotonic(), 3),
            "width": frame["width"],
            "height": frame["height"],
            "size_bytes": len(frame["png"]),
            "cursor": list(cursor_position()),
        }
        if save_dir:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            file_path = os.path.join(save_dir, f"frame-{stamp}-{index:03d}.png")
            with open(file_path, "wb") as fh:
                fh.write(frame["png"])
            item["path"] = os.path.abspath(file_path)
        frames.append(item)
        last_png = frame["png"]
        index += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return {
        "duration_sec": round(duration, 2),
        "interval_sec": round(interval, 2),
        "count": len(frames),
        "frames": frames,
        "last_png": last_png,
    }
