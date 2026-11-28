"""Local tools the AI agent can call: files, terminal, running applications.

The agent runs model-requested actions through this module instead of touching
the disk directly. Restricted modes confine file tools to the open project;
full mode permits computer-wide paths and shell commands under the Windows
user account.

Plain Python on purpose — no Qt imports, fully unit-testable.
"""
from __future__ import annotations

import base64
import json
from io import StringIO
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.subprocess_utils import CREATE_NO_WINDOW, decode_console_output, popen_hidden, refreshed_user_path, run_hidden
from core.file_snapshot import snapshot_bytes, snapshot_file, write_if_unchanged
from services.temp_files import TempFiles
from services.workbench_tools import TOOLS as WORKBENCH_TOOLS

__all__ = ["ToolError", "LocalToolbox", "TOOL_DEFINITIONS"]

_LOG = logging.getLogger("pyscribe.agent_tools")

MAX_READ_CHARS = 60_000
MAX_READ_LINES = 200             # default page size: bounded reads keep the context small
MAX_READ_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_CHARS = 2_000_000     # safety valve against a runaway command only
MAX_SEARCH_RESULTS = 500
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 900
MAX_WRITE_BYTES = 8 * 1024 * 1024

ACCESS_MODES = {
    "read_only": "Только чтение",
    "write_only": "Только запись (с подтверждением)",
    "full": "Полный доступ к компьютеру (без подтверждений CodeLane)",
}
# Compact labels for the header chip; the full text stays in the menu/tooltip.
ACCESS_SHORT = {
    "read_only": "Чтение",
    "write_only": "Запись",
    "full": "Полный",
}


def normalize_access_mode(value):
    return value if value in ACCESS_MODES else "read_only"

# How a tool is confirmed before it runs: read-only tools never ask.
_CONFIRM_POLICY = {
    "find_files": False,
    "view_image": False,
    "apply_patch": True,
    "exec_command": True,
    "write_stdin": True,
    "read_file": False,
    "list_directory": False,
    "search_in_project": False,
    "write_file": True,
    "run_terminal_command": True,
    "open_application": True,
    "run_executable": True,
    "run_admin_command": True,
    "capture_screen": True,
    "screenshot": True,
    "desktop_stream": True,
    "run_project_tests": True,
    "python": True,
    "git": True,
    "pytest": True,
    "pip": True,
    "edit_file": True,
    "computer_control": True,
    "process_monitor": False,
}


def _tool(name: str, description: str, properties: Dict[str, Any],
          required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    _tool("find_files", "Найти пути файлов по glob без чтения содержимого. Учитывает исключения проекта; offset с 0, продолжение в next_offset.",
          {"pattern": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
           "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, []),
    _tool("apply_patch", "Применить патч к нескольким файлам. Формат: *** Begin Patch, *** Add/Update/Delete File: путь, @@ и строки с пробелом/+/-, *** End Patch. Для переноса: *** Move to: путь после Update File. Контекст должен быть однозначным. Все файлы проверяются до записи; открытые вкладки меняются в буфере.",
          {"patch": {"type": "string"}}, ["patch"]),
    _tool("exec_command", "Запустить команду и получить вывод или session_id для продолжения через write_stdin. Сессия ограничена timeout_sec; незавершённые команды останавливаются в конце задачи. Для интерактивных TTY-приложений используй встроенный терминал.",
          {"cmd": {"type": "string"}, "cwd": {"type": "string"},
           "shell": {"type": "string", "enum": ["cmd", "powershell"]},
           "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
           "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 900},
           "max_output_chars": {"type": "integer", "minimum": 256, "maximum": 48000}}, ["cmd"]),
    _tool("write_stdin", "Получить следующий вывод командной сессии или передать текст в stdin. terminate=true останавливает дерево процессов. completed и exit_code подтверждают завершение; дочитай has_more_output.",
          {"session_id": {"type": "string"}, "chars": {"type": "string"},
           "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
           "max_output_chars": {"type": "integer", "minimum": 256, "maximum": 48000},
           "terminate": {"type": "boolean"}}, ["session_id"]),
    _tool("view_image", "Передать локальное изображение PNG, JPEG, GIF или WebP модели для визуальной проверки.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("workspace_state", "Получить актуальное состояние проводника, браузера и анализатора CodeLane. Вызывай повторно после действий пользователя и перед решениями.", {}, []),
    _tool("browser_navigate", "Открыть URL во встроенном браузере CodeLane.", {"url": {"type": "string"}}, ["url"]),
    _tool("analysis_refresh", "Запустить обновление встроенного анализатора проекта. Результат читать через workspace_state.", {}, []),
    _tool(
        "editor_state",
        "Получить активный файл, позицию курсора, выделенный текст и список открытых вкладок CodeLane.",
        {}, [],
    ),
    _tool(
        "editor_cursor",
        "Перейти к строке и столбцу активного редактора CodeLane. Нумерация с 1.",
        {"line": {"type": "integer"}, "column": {"type": "integer"}},
        ["line"],
    ),
    _tool(
        "read_file",
        "Чтение строк кода после search_in_project. Для PDF, DOCX, ODT и XLSX сразу используй read_document. "
        "offset — строка от 1, limit — число строк (по умолчанию 200), "
        "0 — без лимита строк в пределах страницы, column — позиция продолжения длинной строки из ответа.",
        {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"},
         "column": {"type": "integer", "minimum": 0}},
        ["path"],
    ),
    _tool(
        "read_document",
        "Чтение PDF, DOCX, ODT и XLSX. offset и limit задают страницы PDF или строки документа. "
        "Начни с 1–5 страниц или строк и продолжай с next_offset при необходимости.",
        {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
         "limit": {"type": "integer", "minimum": 1}},
        ["path"],
    ),
    _tool(
        "open_file",
        "Открыть файл проекта в редакторе CodeLane; внешний файл доступен для фонового чтения.",
        {"path": {"type": "string", "description": "Путь к файлу относительно корня проекта"}},
        ["path"],
    ),
    _tool(
        "list_directory",
        "Содержимое каталога. Точка означает корень проекта.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _tool(
        "search_in_project",
        "Основной инструмент изучения кода. Задай regex=true для селекторов, определений и вызовов. "
        "Ограничь вывод через glob, context_lines и max_results. query или queries (ИЛИ). "
        "Возвращает пути, номера и нужные строки.",
        {"query": {"type": "string"}, "glob": {"type": "string"}, "regex": {"type": "boolean"},
         "queries": {"type": "array", "items": {"type": "string"}},
         "context_lines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
         "case_sensitive": {"type": "boolean", "default": False},
         "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 40}},
        [],
    ),
    _tool(
        "write_file",
        "Создать или перезаписать файл целиком.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Заменить единственное точное совпадение. Открытый файл меняется в буфере редактора.",
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"],
    ),
    _tool(
        "run_terminal_command",
        "Команда Windows, включая python script.py. Возвращает вывод и код завершения.",
        {"command": {"type": "string"}, "shell": {"type": "string", "enum": ["cmd", "powershell"]},
         "cwd": {"type": "string", "description": "По умолчанию корень проекта"},
         "timeout_sec": {"type": "integer", "default": 30}},
        ["command"],
    ),
    _tool(
        "open_application",
        "Запустить приложение или утилиту (например, тестовый сервер или GUI-клиент). "
        "Требует подтверждения пользователя.",
        {"program": {"type": "string", "description": "Исполняемый файл или команда"},
         "args": {"type": "string", "description": "Аргументы через пробел"}},
        ["program"],
    ),
    _tool("capture_screen", "Сделать снимок экрана и показать его модели. Каждый снимок требует подтверждения пользователя.", {}, []),
    _tool(
        "screenshot",
        "Сделать снимок экрана компьютера и сохранить его в PNG (по умолчанию в папку screenshoots проекта). "
        "Возвращает путь к файлу и размеры. Требует подтверждения пользователя.",
        {
            "path": {"type": "string", "description": "Куда сохранить PNG (необязательно)"},
            "region": {"type": "object", "description": "Область {x,y,width,height} виртуального экрана"},
            "max_width": {"type": "integer", "minimum": 320, "maximum": 1920},
        },
        [],
    ),
    _tool(
        "desktop_stream",
        "Живой просмотр экрана в реальном времени с параллельным диспетчером задач. "
        "Снимает серию кадров и одновременно наблюдает за процессами, возвращая совмещённую "
        "ленту кадров и метрик CPU/RAM/топ процессов; последний кадр показывается как изображение. "
        "Кадры сохраняются на диск. Требует подтверждения пользователя.",
        {
            "duration_sec": {"type": "number", "minimum": 0.2, "maximum": 30},
            "interval_sec": {"type": "number", "minimum": 0.1, "maximum": 5},
            "max_width": {"type": "integer", "minimum": 320, "maximum": 1920},
            "save_screenshots": {"type": "boolean"},
            "top": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        [],
    ),
    _tool(
        "computer_control",
        "Управлять компьютером в реальном времени: мышь, клавиатура, окна и снимок экрана после действий. "
        "actions — список шагов, выполняемых по порядку: "
        "move(x,y), click(x,y,button,count), double_click, right_click, mouse_down, mouse_up, "
        "drag(x,y,to_x,to_y), scroll(amount), type(text), key(keys), hotkey(keys), wait(seconds), "
        "focus_window(title), screenshot. Координаты — пиксели виртуального экрана; "
        "точку для клика бери из последнего снимка. Возвращает изображение экрана и состояние процессов. "
        "Требует подтверждения пользователя (одно на серию действий).",
        {
            "actions": {"type": "array", "items": {"type": "object"},
                        "description": "Шаги управления по порядку"},
            "screenshot": {"type": "boolean",
                           "description": "Снять финальный кадр и вернуть его модели"},
            "screenshot_after_each": {"type": "boolean",
                                      "description": "Сохранять кадр после каждого шага (файлы в отчёте)"},
            "max_width": {"type": "integer", "minimum": 320, "maximum": 1920},
            "interval_sec": {"type": "number", "minimum": 0, "maximum": 5},
            "watch_sec": {"type": "number", "minimum": 0, "maximum": 30,
                          "description": "Сколько секунд следить за процессами после действий"},
        },
        ["actions"],
    ),
    _tool("desktop_state", "Список видимых окон компьютера, активное окно, положение курсора и размеры экрана. "
          "Используй перед focus_window или кликами; управление выполняй через computer_control.", {}, []),
    _tool(
        "process_monitor",
        "Живые метрики ПК: загрузка CPU/RAM, топ процессов, поиск процессов по имени и наблюдение "
        "за стартом/остановкой процессов. action=snapshot|find|watch.",
        {
            "action": {"type": "string", "enum": ["snapshot", "find", "watch"]},
            "pattern": {"type": "string", "description": "Регулярное выражение по имени/exe/cmdline"},
            "top": {"type": "integer", "minimum": 1, "maximum": 50},
            "duration_sec": {"type": "number", "minimum": 0.5, "maximum": 30},
            "interval_sec": {"type": "number", "minimum": 0.2, "maximum": 5},
        },
        ["action"],
    ),
    _tool("run_admin_command", "Запустить CMD или PowerShell с правами администратора через Windows UAC. Вывод не перехватывается.",
          {"command": {"type": "string"}, "shell": {"type": "string", "enum": ["cmd", "powershell"]}}, ["command"]),
    _tool("run_executable", "Запустить исполняемый файл проекта без оболочки. Требует подтверждения пользователя.",
          {"path": {"type": "string"}, "args": {"type": "string"}}, ["path"]),
    _tool("run_project_tests", "Запустить тесты проекта локально (pytest или npm test) и вернуть код выхода и вывод.",
          {"target": {"type": "string"}, "timeout_sec": {"type": "integer"}}, []),
    _tool("python", "Запустить Python открытого проекта. code выполняет короткий код через -c, path запускает файл .py, args добавляет аргументы. Возвращает код возврата и вывод.",
          {"code": {"type": "string"}, "path": {"type": "string"},
           "args": {"type": "string"}, "timeout_sec": {"type": "integer"}}, []),
    _tool("pytest", "Запустить pytest открытого проекта через интерпретатор проекта. target — файл или каталог тестов, args — параметры pytest (например -k agent). Возвращает код возврата и вывод.",
          {"target": {"type": "string"}, "args": {"type": "string"},
           "timeout_sec": {"type": "integer"}}, []),
    _tool("pip", "Управлять пакетами проекта через pip выбранного интерпретатора. packages — имена пакетов через пробел, args — дополнительные параметры pip.",
          {"action": {"type": "string",
                      "enum": ["install", "uninstall", "list", "show", "freeze", "check", "download"]},
           "packages": {"type": "string"}, "args": {"type": "string"},
           "timeout_sec": {"type": "integer"}}, ["action"]),
    _tool("git", "Выполнить команду Git в открытом проекте (status, diff, log, show, branch, add, commit, push и др.). args — аргументы без слова git, например \"status --short\".",
          {"args": {"type": "string"}, "timeout_sec": {"type": "integer"}}, ["args"]),
]


TOOL_DEFINITIONS.extend(
    _tool(name, description, properties, required)
    for name, (_, description, properties, required) in WORKBENCH_TOOLS.items()
)


class ToolError(RuntimeError):
    """Raised for a rejected or failed tool call (message is user-facing)."""


class LocalToolbox:
    """Executes agent tool calls inside one project root."""

    def __init__(self, project_root: Optional[str],
                 confirm: Optional[Callable[[str, str, str], bool]] = None,
                 allow_write: bool = True,
                 allow_commands: bool = True,
                 access_mode: str = "full",
                 ui_action: Optional[Callable[[str, Dict[str, Any]], str]] = None,
                 should_stop: Optional[Callable[[], bool]] = None,
                 clean_temp_files: bool = True):
        self.root = Path(project_root).resolve() if project_root else None
        self.confirm = confirm
        self.allow_write = allow_write
        self.allow_commands = allow_commands
        self.access_mode = normalize_access_mode(access_mode)
        self.ui_action = ui_action
        self.should_stop = should_stop
        self.background = False
        self.clean_temp_files = bool(clean_temp_files)
        # Разрешение на управление мышью/клавиатурой: выдаётся один раз на
        # серию действий и живёт CONTROL_GRANT_SECONDS секунд.
        self._control_grant_until = 0.0
        self._control_actions_left = 0
        self.removed_temp_files: List[str] = []
        self._temp_files: Optional[TempFiles] = None
        from services.agent_process import CommandSessions
        self.command_sessions = CommandSessions()

    def _allowed(self, name):
        if name in WORKBENCH_TOOLS:
            if self.ui_action is None:
                return False
            access = WORKBENCH_TOOLS[name][0]
            if access == "read":
                return self.access_mode != "write_only"
            if access == "write":
                return self.allow_write and self.access_mode != "read_only"
            return self.access_mode == "full" and (access != "command" or self.allow_commands)
        if name == "workspace_state":
            return self.access_mode != "write_only"
        if name in ("browser_navigate", "analysis_refresh"):
            return self.access_mode == "full"
        if name in ("read_file", "read_document", "list_directory", "search_in_project", "find_files", "view_image",
                    "open_file", "editor_state", "editor_cursor"):
            return self.access_mode != "write_only"
        if name in ("write_file", "edit_file", "apply_patch"):
            return self.allow_write and self.access_mode != "read_only"
        if name in ("run_terminal_command", "open_application", "run_executable", "run_admin_command",
                    "run_project_tests", "python", "git", "pytest", "pip", "exec_command", "write_stdin"):
            return self.allow_commands and self.access_mode == "full"
        if name == "computer_control":
            # Живое управление вводом — только полный доступ и Windows.
            return self.allow_commands and self.access_mode == "full" and os.name == "nt"
        if name == "desktop_state":
            return self.access_mode == "full" and os.name == "nt"
        if name == "process_monitor":
            # Только чтение метрик: разрешено в любом режиме доступа.
            return True
        if name in ("screenshot", "desktop_stream"):
            # Съёмка экрана компьютера и сохранение файлов — только полный доступ.
            return self.access_mode == "full" and os.name == "nt"
        if name == "capture_screen":
            return self.ui_action is not None and self.access_mode == "full"
        return False

    # -- helpers -------------------------------------------------------
    def definitions(self) -> List[Dict[str, Any]]:
        """Tool definitions filtered by the user's permission settings."""
        allowed = []
        for definition in TOOL_DEFINITIONS:
            name = definition["function"]["name"]
            if not self._allowed(name):
                continue
            allowed.append(definition)
        return allowed

    def _resolve(self, raw: str) -> Path:
        if self.root is None and self.access_mode != "full":
            raise ToolError("Проект не открыт — работа с файлами недоступна.")
        text = str(raw or ".").strip().strip('"').strip("'")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = (self.root or Path.cwd()) / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"Не удалось разрешить путь: {exc}") from exc
        if self.access_mode != "full" and resolved != self.root and self.root not in resolved.parents:
            raise ToolError("Путь вне проекта: доступ разрешён только внутри проекта.")
        return resolved

    def _outside_project(self, path: Path) -> bool:
        return self.root is None or (path != self.root and self.root not in path.parents)

    def _ask(self, name: str, details: str, title: str = "") -> None:
        """Ask the user to confirm a mutating action.

        "Полный доступ" means exactly that: the agent works without per-action
        prompts. Only the restricted modes ask (and there is a settings switch
        to disable edits/commands entirely).
        """
        # Черновые файлы появляются именно на изменяющих шагах, поэтому снимок
        # берётся перед первым из них, а не при открытии проекта.
        self._track_temp_files()
        if not _CONFIRM_POLICY.get(name, True):
            return
        if self.access_mode == "full":
            return
        if self.confirm is None:
            raise ToolError("Действие отклонено: подтверждение недоступно.")
        if not self.confirm(name, title or name, details):
            raise ToolError("Пользователь отклонил действие.")

    # -- tools ---------------------------------------------------------
    def close(self):
        self.command_sessions.close()
        self.removed_temp_files = self._temp_files.cleanup() if self._temp_files is not None else []
        if self.removed_temp_files:
            _LOG.info("Удалены временные файлы агента: %s", ", ".join(self.removed_temp_files))
        self._temp_files = None

    def _track_temp_files(self) -> None:
        """Запомнить временные файлы проекта до первой изменяющей операции."""
        if not self.clean_temp_files or self._temp_files is not None or self.root is None:
            return
        self._temp_files = TempFiles(self.root)

    def read_version(self, name, args):
        """Check disk/editor revisions without reading file contents again."""
        if not self._allowed(name):
            raise ToolError("Чтение запрещено настройками доступа.")
        target = self._resolve(args["path"])
        stat = target.stat()
        buffer = None
        if name == "read_file" and self.ui_action and not self._outside_project(target):
            buffer = self.ui_action("buffer_revision", {"path": str(target)})
        return (str(target), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino, buffer)

    def find_files(self, pattern="*", offset=0, limit=100):
        from services.project_search import find_project_files
        return find_project_files(self.root, pattern=pattern, offset=offset, limit=limit,
                                  should_stop=self.should_stop)

    def apply_patch(self, patch):
        from services.agent_patch import apply_patch
        return apply_patch(self, patch)

    def view_image(self, path):
        import base64
        target = self._resolve(path)
        self._check_read_cancelled()
        if not self._allowed("view_image"):
            raise ToolError("Чтение изображений запрещено настройками доступа.")
        if not target.is_file() or target.stat().st_size > 10 * 1024 * 1024:
            raise ToolError("Нужен файл изображения размером до 10 МБ.")
        with target.open("rb") as source:
            data = source.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ToolError("Изображение превышает 10 МБ.")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ToolError("Поддерживаются изображения PNG, JPEG, GIF и WebP.")
        return {"__image__": base64.b64encode(data).decode("ascii"), "media_type": mime,
                "description": f"Изображение {path} передано модели."}

    def read_file(self, path: str, offset: int = 0, limit: Optional[int] = None,
                  column: int = 0) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        from services.workbench_documents import DOCUMENT_SUFFIXES
        if target.suffix.lower() in DOCUMENT_SUFFIXES:
            return self.read_document(path, offset=max(1, int(offset or 1)), limit=min(int(limit or 5), 5))
        first = max(1, int(offset or 1))
        count = MAX_READ_LINES if limit is None else max(0, int(limit))
        column = max(0, int(column))
        text = None
        buffer_more = False
        page_from_editor = False
        if self.ui_action is not None and not self._outside_project(target):
            try:
                buffer = self.ui_action("read_buffer", {
                    "path": str(target), "first": first, "count": count,
                    "column": column, "characters": MAX_READ_CHARS + 1,
                })
                if isinstance(buffer, dict):
                    text, buffer_more = buffer["page"], buffer["more"]
                    page_from_editor = True
                elif buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    text = buffer
            except Exception:
                # The disk copy remains a useful fallback if a UI is closing.
                text = None
        more = False
        try:
            source = (StringIO(text, newline=None) if text is not None else
                      target.open(encoding="utf-8", errors="replace", newline=None))
            with source:
                if not page_from_editor:
                    for _ in range(first - 1):
                        while True:
                            self._check_read_cancelled()
                            piece = source.readline(65_536)
                            if not piece:
                                return "[Строки в указанном диапазоне отсутствуют.]"
                            if piece.endswith("\n") or len(piece) < 65_536:
                                break
                    remaining_column = column
                    while remaining_column:
                        self._check_read_cancelled()
                        piece = source.readline(min(max(1, remaining_column // 2), 65_536))
                        if not piece or piece.endswith("\n"):
                            return "[Символы в указанном диапазоне отсутствуют.]"
                        remaining_column -= len(piece.encode("utf-16-le")) // 2
                        if remaining_column < 0:
                            return "[Символы в указанном диапазоне отсутствуют.]"

                pieces = []
                remaining = MAX_READ_CHARS
                while (not count or len(pieces) < count) and remaining:
                    self._check_read_cancelled()
                    line = source.readline(remaining)
                    if not line:
                        break
                    pieces.append(line)
                    remaining -= len(line)
                partial_line = bool(pieces and not pieces[-1].endswith("\n") and (not remaining or buffer_more))
                if limit in (0, None) or len(pieces) < count or partial_line:
                    more = bool(source.read(1)) or buffer_more
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать файл: {exc}") from exc

        answer = "".join(pieces)
        if not pieces:
            return "[Строки в указанном диапазоне отсутствуют.]" if offset or limit or column else ""
        if offset or limit or column or more:
            answer = f"[строки {first}–{first + len(pieces) - 1}]\n" + answer.rstrip("\n")
        if more:
            next_line = first + len(pieces)
            next_column = 0
            if partial_line:
                next_line -= 1
                next_column = (column if len(pieces) == 1 else 0) + len(pieces[-1].encode("utf-16-le")) // 2
            continuation = f", column={next_column}" if next_column else ""
            answer += ("\n[Достигнут предел размера ответа. "
                       f"Продолжение: read_file(path={path!r}, offset={next_line}, "
                       f"limit={count or MAX_READ_LINES}{continuation}).]")
        return answer

    def _check_read_cancelled(self):
        if self.should_stop and self.should_stop():
            raise ToolError("Запрос остановлен.")

    def read_document(self, path: str, offset: int = 1, limit: int = 5) -> str:
        from services.workbench_documents import DOCUMENT_SUFFIXES, read_document

        self._check_read_cancelled()
        if not self._allowed("read_document"):
            raise ToolError("Чтение документов запрещено настройками доступа агента.")
        target = self._resolve(path)
        if type(offset) is not int or type(limit) is not int or min(offset, limit) < 1:
            raise ToolError("offset и limit должны быть положительными целыми числами.")
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        if target.suffix.lower() not in DOCUMENT_SUFFIXES:
            from core.ignore_rules import BINARY_SUFFIXES

            if target.suffix.lower() in BINARY_SUFFIXES:
                raise ToolError(f"Формат файла {target.suffix} не поддерживается для чтения документа.")
            return self.read_file(path, offset=offset, limit=limit)
        page = read_document(target, offset=offset, limit=limit, check_cancelled=self._check_read_cancelled)
        return json.dumps(page, ensure_ascii=False)

    def open_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        if self.background or self._outside_project(target):
            return f"Файл доступен для фоновой работы: {target}. Используй read_file для чтения."
        if self.ui_action is None:
            raise ToolError("Открытие вкладки CodeLane недоступно в текущем режиме.")
        return self.ui_action("open_file", {"path": str(target)})

    def list_directory(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"Каталог не найден: {path}")
        try:
            from itertools import islice

            with os.scandir(target) as entries:
                sample = list(islice(entries, 2001))
            entries = sorted(sample[:2000], key=lambda p: (not p.is_dir(follow_symlinks=False), p.name.lower()))
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать каталог: {exc}") from exc
        lines = []
        for entry in entries:
            if self.access_mode != "full" and entry.name.startswith(".") and entry.name not in (".pyscribe",):
                continue
            suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
            lines.append(f"{entry.name}{suffix}")
        if len(sample) > 2000:
            lines.append("… (показаны первые 2000 записей; уточни подкаталог или воспользуйся поиском)")
        if not lines:
            return "(пусто)"
        return "\n".join(lines)

    def search_in_project(self, query: str = "", glob: str = "", regex: bool = False,
                          queries: Optional[List[str]] = None, context_lines: int = 2,
                          max_results: int = 40, case_sensitive: bool = False,
                          output_mode: str = "content") -> str:
        from services.project_search import search_project
        return search_project(self.root, query=query, glob=glob, regex=regex,
                              queries=queries, context_lines=context_lines,
                              max_results=max_results, should_stop=self.should_stop,
                              case_sensitive=case_sensitive, output_mode=output_mode)

    def exec_command(self, cmd, cwd="", shell="powershell", yield_time_ms=1000,
                     timeout_sec=300, max_output_chars=12000):
        if not self._allowed("exec_command"):
            raise ToolError("Выполнение команд запрещено настройками доступа.")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ToolError("Нужна непустая команда cmd.")
        if shell not in {"cmd", "powershell"}:
            raise ToolError("Укажи оболочку cmd или powershell.")
        if type(timeout_sec) is not int or not 1 <= timeout_sec <= MAX_TIMEOUT:
            raise ToolError("timeout_sec должен быть от 1 до 900.")
        if (type(yield_time_ms) is not int or not 0 <= yield_time_ms <= 10000
                or type(max_output_chars) is not int or not 256 <= max_output_chars <= 48000):
            raise ToolError("Проверь yield_time_ms и max_output_chars.")
        self._check_read_cancelled()
        directory = self._resolve(cwd) if cwd else self.root or Path.cwd()
        if not directory.is_dir():
            raise ToolError("Рабочий каталог не найден.")
        if shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if not executable:
                raise ToolError("PowerShell не найден.")
            launch, shell_mode = [executable, "-NoProfile", "-NonInteractive", "-Command", cmd], False
        else:
            launch, shell_mode = cmd, True
        self._ask("exec_command", f"{directory}\n{cmd}", "Запустить команду")
        environment = os.environ.copy()
        environment["PATH"] = refreshed_user_path(environment.get("PATH", ""))
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        session_id = self.command_sessions.start(launch, shell=shell_mode, cwd=directory,
            env=environment, timeout=timeout_sec, should_stop=self.should_stop, command=cmd)
        return json.dumps(self.command_sessions.poll(session_id, yield_time_ms=yield_time_ms,
                                                    max_output_chars=max_output_chars), ensure_ascii=False)

    def write_stdin(self, session_id, chars="", yield_time_ms=1000, max_output_chars=12000,
                    terminate=False):
        if not self._allowed("write_stdin"):
            raise ToolError("Управление командами запрещено настройками доступа.")
        return json.dumps(self.command_sessions.poll(session_id, chars=chars, yield_time_ms=yield_time_ms,
            max_output_chars=max_output_chars, terminate=terminate), ensure_ascii=False)

    def write_file(self, path: str, content: str) -> str:
        if not self.allow_write:
            raise ToolError("Редактирование файлов отключено в настройках агента.")
        target = self._resolve(path)
        payload = str(content)
        if len(payload.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError("Содержимое слишком большое для записи.")
        expected = snapshot_file(target)
        existed = expected is not None
        preview = payload if len(payload) <= 1200 else payload[:1200] + "\n… (обрезано)"
        self._ask("write_file",
                  f"Файл: {target}\nРежим: {'перезапись' if existed else 'создание'}\n\n{preview}",
                  "Изменить файл проекта")
        if self.ui_action is not None and existed and not self._outside_project(target):
            try:
                if not self.background:
                    self.ui_action("open_file", {"path": str(target)})
                buffer = self.ui_action("read_buffer", {"path": str(target)})
                if buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    return self.ui_action("replace_buffer", {
                        "path": str(target), "expected": buffer, "content": payload,
                    })
            except Exception as exc:
                raise ToolError(f"Не удалось обновить открытую вкладку: {exc}") from exc
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_if_unchanged(target, payload.encode("utf-8"), expected)
        except OSError as exc:
            raise ToolError(f"Не удалось записать файл: {exc}") from exc
        if self.ui_action is not None and not self.background and not self._outside_project(target):
            self.ui_action("open_file", {"path": str(target)})
        return f"{'Обновлён' if existed else 'Создан'} файл {path} ({len(payload)} символов)."

    def _edit_source(self, path):
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        try:
            if target.stat().st_size > MAX_READ_BYTES:
                raise ToolError(f"Файл больше {MAX_READ_BYTES // (1024 * 1024)} МБ.")
            data = target.read_bytes()
            expected = snapshot_bytes(data)
            current = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать файл: {exc}") from exc
        if self.ui_action is not None and not self._outside_project(target):
            try:
                buffer = self.ui_action("read_buffer", {"path": str(target)})
                if buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    current = buffer
            except Exception:
                pass
        return target, data, expected, current

    def preview_edit_source(self, path):
        if not self._allowed("edit_file") or self.access_mode == "write_only":
            raise ToolError("Разрешите чтение и правку файлов в настройках агента.")
        target, _, _, current = self._edit_source(path)
        return (str(target) if self._outside_project(target) else target.relative_to(self.root).as_posix()), current

    def edit_file(self, path: str, old_text: str, new_text: str, *, expected_source=None) -> str:
        if not self._allowed("edit_file"):
            raise ToolError("Редактирование файлов отключено в настройках агента.")
        if not old_text and expected_source != "":
            raise ToolError("Исходный фрагмент не может быть пустым.")
        if len(str(new_text).encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError("Новый фрагмент слишком большой.")
        target, data, expected, current = self._edit_source(path)
        if expected_source is not None and current != expected_source:
            raise ToolError("Файл изменился после подготовки правки. Пересоберите изменения.")
        count = current.count(old_text)
        if count != 1:
            raise ToolError(
                f"Правка отменена: исходный фрагмент должен встречаться ровно один раз "
                f"(найдено: {count}). Перечитай актуальный файл и уточни фрагмент.")
        updated = current.replace(str(old_text), str(new_text), 1)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError("Итоговый файл слишком большой для безопасной правки.")
        self._ask("edit_file", f"Файл: {target}\n\n--- было ---\n{old_text[:1200]}"
                  f"\n\n--- станет ---\n{str(new_text)[:1200]}",
                  "Применить правку в редакторе")
        if self.should_stop and self.should_stop():
            raise ToolError("Правка отменена.")
        if self.ui_action is not None and not self._outside_project(target):
            if self.background:
                buffer = self.ui_action("read_buffer", {"path": str(target)})
                if buffer == "__NO_OPEN_EDITOR_BUFFER__":
                    data.decode("utf-8")
                    write_if_unchanged(target, updated.encode("utf-8"), expected)
                    return f"Изменён файл {path}."
            payload = {
                "path": str(target), "old_text": str(old_text),
                "new_text": str(new_text), "expected": current,
            }
            if expected_source is not None:
                payload["_project_root"] = str(self.root)
            return self.ui_action("edit_file", payload)
        try:
            data.decode("utf-8")
            write_if_unchanged(target, updated.encode("utf-8"), expected)
        except UnicodeDecodeError as exc:
            raise ToolError("Откройте файл в редакторе для правки текста в этой кодировке.") from exc
        except OSError as exc:
            raise ToolError(f"Не удалось изменить файл: {exc}") from exc
        return f"Изменён файл {path}."

    def run_terminal_command(self, command: str, timeout_sec: int = DEFAULT_TIMEOUT,
                             shell: str = "cmd", cwd: str = "") -> str:
        if not self.allow_commands:
            raise ToolError("Выполнение команд отключено в настройках агента.")
        if self.root is None and self.access_mode != "full":
            raise ToolError("Проект не открыт.")
        command = str(command or "").strip()
        if not command:
            raise ToolError("Пустая команда.")
        shell = str(shell or "cmd").lower()
        if shell not in {"cmd", "powershell"}:
            raise ToolError("Укажи оболочку cmd или powershell.")
        working_directory = self._resolve(cwd) if cwd else self.root or Path.cwd()
        if not working_directory.is_dir():
            raise ToolError(f"Рабочий каталог не найден: {working_directory}")
        self._ask("run_terminal_command", f"{shell} · {working_directory}\n$ {command}",
                  "Выполнить команду")
        if shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable is None:
                raise ToolError("PowerShell не найден на компьютере.")
            launch, shell_mode = [executable, "-NoProfile", "-NonInteractive", "-Command", command], False
        else:
            launch, shell_mode = command, True
        timeout = max(1, min(int(timeout_sec or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        process = None
        was_stopped = False
        timed_out = False
        environment = os.environ.copy()
        environment["PATH"] = refreshed_user_path(environment.get("PATH", ""))

        def stop_process_tree():
            if process is None or process.poll() is not None:
                return
            if os.name == "nt":
                try:
                    run_hidden(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=5)
                    return
                except (OSError, subprocess.SubprocessError):
                    pass
            process.terminate()

        try:
            # File-backed capture prevents a verbose process from growing RAM
            # while the AI worker waits for it. Polling also lets Stop interrupt
            # the child promptly instead of waiting for the full command timeout.
            with tempfile.TemporaryFile() as capture:
                process = popen_hidden(
                    launch, shell=shell_mode, cwd=str(working_directory),
                    stdout=capture, stderr=subprocess.STDOUT, env=environment,
                )
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self.should_stop is not None and self.should_stop():
                        was_stopped = True
                        stop_process_tree()
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        stop_process_tree()
                        break
                    try:
                        process.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                if process.poll() is None:
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                capture.seek(0)
                output = decode_console_output(capture.read(MAX_OUTPUT_CHARS * 4 + 1))
                output = output.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
        except OSError as exc:
            raise ToolError(f"Не удалось запустить команду: {exc}") from exc
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n… (вывод обрезан)"
        if was_stopped:
            return "Команда остановлена пользователем.\n" + (output.strip() or "(нет вывода)")
        if timed_out:
            return f"Команда не завершилась за {timeout} с и была остановлена.\n{output.strip()}".strip()
        return f"Код возврата: {process.returncode}\n{output.strip() or '(нет вывода)'}"

    # -- живое управление компьютером --------------------------------
    CONTROL_ACTIONS = (
        "move", "click", "double_click", "right_click", "middle_click",
        "mouse_down", "mouse_up", "drag", "scroll", "type", "key", "hotkey",
        "wait", "screenshot", "focus_window",
    )
    CONTROL_GRANT_SECONDS = 600
    CONTROL_ACTIONS_PER_GRANT = 120

    def _normalize_control_steps(self, actions) -> List[Dict[str, Any]]:
        """Проверить сценарий управления до первого действия."""
        if isinstance(actions, dict):
            actions = [actions]
        if not isinstance(actions, (list, tuple)) or not actions:
            raise ToolError("actions должен быть непустым списком шагов управления.")
        if len(actions) > 40:
            raise ToolError("Не более 40 действий за один вызов computer_control.")
        steps: List[Dict[str, Any]] = []
        for index, raw in enumerate(actions, start=1):
            if not isinstance(raw, dict):
                raise ToolError(f"Шаг {index}: ожидался объект JSON.")
            kind = str(raw.get("type") or raw.get("action") or "").strip().lower()
            if kind not in self.CONTROL_ACTIONS:
                raise ToolError(
                    f"Шаг {index}: неизвестный тип {kind!r}. "
                    f"Допустимо: {', '.join(self.CONTROL_ACTIONS)}.")
            step: Dict[str, Any] = {"type": kind}
            if kind in ("move", "click", "double_click", "right_click", "middle_click"):
                if kind != "move" and raw.get("x") is None and raw.get("y") is None:
                    step["x"] = step["y"] = None
                else:
                    if raw.get("x") is None or raw.get("y") is None:
                        raise ToolError(f"Шаг {index}: нужны координаты x и y.")
                    step["x"], step["y"] = raw["x"], raw["y"]
                if kind == "click":
                    step["button"] = str(raw.get("button") or "left").lower()
                    step["count"] = raw.get("count", 1)
            elif kind == "drag":
                if any(raw.get(key) is None for key in ("x", "y", "to_x", "to_y")):
                    raise ToolError(f"Шаг {index}: drag требует x, y, to_x, to_y.")
                step.update({key: raw[key] for key in ("x", "y", "to_x", "to_y")})
                step["button"] = str(raw.get("button") or "left").lower()
                step["duration"] = raw.get("duration", 0.35)
            elif kind == "scroll":
                if raw.get("amount") is None:
                    raise ToolError(f"Шаг {index}: scroll требует amount (положительное — вверх).")
                step["amount"] = raw["amount"]
                step["horizontal"] = bool(raw.get("horizontal"))
            elif kind == "type":
                text = raw.get("text")
                if not isinstance(text, str) or not text:
                    raise ToolError(f"Шаг {index}: type требует непустой text.")
                step["text"] = text
                step["interval"] = raw.get("interval", 0.0)
            elif kind in ("key", "hotkey"):
                keys = raw.get("keys")
                if isinstance(keys, str):
                    keys = [keys]
                if not isinstance(keys, list) or not keys:
                    raise ToolError(f"Шаг {index}: {kind} требует keys ('ctrl+s' или список).")
                step["keys"] = keys
            elif kind == "wait":
                step["seconds"] = raw.get("seconds", 0.5)
            elif kind == "focus_window":
                title = str(raw.get("title") or "").strip()
                hwnd = raw.get("hwnd")
                if not title and not hwnd:
                    raise ToolError(f"Шаг {index}: focus_window требует title или hwnd.")
                step["title"] = title or None
                step["hwnd"] = hwnd
                step["exact"] = bool(raw.get("exact"))
            steps.append(step)
        return steps

    def _describe_control_steps(self, steps: List[Dict[str, Any]]) -> str:
        labels = {
            "move": "курсор", "click": "клик", "double_click": "двойной клик",
            "right_click": "правый клик", "middle_click": "клик средней кнопкой",
            "mouse_down": "зажать кнопку", "mouse_up": "отпустить кнопку",
            "drag": "перетаскивание", "scroll": "прокрутка", "type": "ввод текста",
            "key": "нажатие клавиш", "hotkey": "горячие клавиши", "wait": "пауза",
            "screenshot": "снимок экрана", "focus_window": "активация окна",
        }
        lines = []
        for index, step in enumerate(steps, start=1):
            kind = step["type"]
            detail = ""
            if step.get("x") is not None and step.get("y") is not None:
                detail = f" ({step['x']}, {step['y']})"
            if kind == "type":
                text = step["text"]
                detail = f": {text[:60]!r}" + ("…" if len(text) > 60 else "")
            elif kind in ("key", "hotkey"):
                detail = ": " + " ".join(str(item) for item in step["keys"])[:60]
            elif kind == "focus_window":
                detail = f": {step.get('title') or step.get('hwnd')}"
            elif kind == "wait":
                detail = f": {step['seconds']} с"
            elif kind == "scroll":
                detail = f": {step['amount']}"
            lines.append(f"{index}) {labels[kind]}{detail}")
        return "\n".join(lines)

    def _control_approval(self, steps: List[Dict[str, Any]]) -> None:
        """Спросить разрешение на управление вводом (одно на серию)."""
        now = time.monotonic()
        granted = now < self._control_grant_until and self._control_actions_left > 0
        if granted:
            self._control_actions_left -= len(steps)
            return
        if self.confirm is None:
            raise ToolError(
                "Управление компьютером недоступно: нет канала подтверждения пользователя.")
        details = ("Агент будет управлять мышью и клавиатурой вашего компьютера.\n"
                   + self._describe_control_steps(steps))
        if len(details) > 1200:
            details = details[:1200] + "\n…"
        if not self.confirm("computer_control",
                            "Разрешить управление компьютером?", details):
            raise ToolError("Пользователь отклонил управление компьютером.")
        self._control_grant_until = now + self.CONTROL_GRANT_SECONDS
        self._control_actions_left = self.CONTROL_ACTIONS_PER_GRANT - len(steps)

    def _apply_control_step(self, desktop, step: Dict[str, Any]) -> Dict[str, Any]:
        kind = step["type"]
        if kind == "move":
            return desktop.mouse_move(step["x"], step["y"])
        if kind == "click":
            return desktop.mouse_click(step.get("button") or "left", step.get("count", 1),
                                       step.get("x"), step.get("y"))
        if kind == "double_click":
            return desktop.mouse_click("left", 2, step.get("x"), step.get("y"))
        if kind == "right_click":
            return desktop.mouse_click("right", 1, step.get("x"), step.get("y"))
        if kind == "middle_click":
            return desktop.mouse_click("middle", 1, step.get("x"), step.get("y"))
        if kind == "mouse_down":
            return desktop.mouse_down("left")
        if kind == "mouse_up":
            return desktop.mouse_up("left")
        if kind == "drag":
            return desktop.mouse_drag(step["x"], step["y"], step["to_x"], step["to_y"],
                                       step.get("button") or "left",
                                       step.get("duration", 0.35))
        if kind == "scroll":
            return desktop.mouse_scroll(step["amount"], bool(step.get("horizontal")))
        if kind == "type":
            return desktop.type_text(step["text"], step.get("interval", 0.0))
        if kind in ("key", "hotkey"):
            return desktop.press_keys(step["keys"])
        if kind == "wait":
            seconds = max(0.0, min(10.0, float(step.get("seconds", 0.5))))
            time.sleep(seconds)
            return {"waited": seconds}
        if kind == "focus_window":
            return desktop.focus_window(step.get("title"), step.get("hwnd"),
                                        bool(step.get("exact")))
        if kind == "screenshot":
            return {"screenshot": "будет снят после шага"}
        raise ToolError(f"Действие {kind} не поддержано.")

    def computer_control(self, actions, screenshot: bool = True,
                         screenshot_after_each: bool = False,
                         max_width: int = 1280, interval_sec: float = 0.25,
                         watch_sec: float = 0.0):
        """Живое управление ПК: серия действий + снимок + состояние процессов."""
        if not self._allowed("computer_control"):
            raise ToolError("Управление компьютером доступно только в режиме полного доступа.")
        from core import desktop_control as desktop

        if not desktop.IS_WINDOWS:
            raise ToolError("Живое управление компьютером реализовано для Windows.")
        steps = self._normalize_control_steps(actions)
        self._control_approval(steps)
        try:
            interval = max(0.0, min(5.0, float(interval_sec)))
        except (TypeError, ValueError):
            interval = 0.25
        try:
            width = max(320, min(1920, int(max_width)))
        except (TypeError, ValueError):
            width = 1280

        started = time.monotonic()
        report: List[Dict[str, Any]] = []
        frames: List[str] = []
        frame_dir = Path(tempfile.gettempdir()) / "CodeLane" / "control"
        for index, step in enumerate(steps, start=1):
            if self.should_stop is not None and self.should_stop():
                report.append({"step": index, "type": step["type"], "status": "остановлено"})
                break
            try:
                result = self._apply_control_step(desktop, step)
                report.append({"step": index, "type": step["type"], "ok": True,
                               "result": result})
            except desktop.DesktopControlError as exc:
                report.append({"step": index, "type": step["type"], "ok": False,
                               "error": str(exc)})
                break
            if step["type"] != "wait" and interval:
                time.sleep(interval)
            if screenshot_after_each and len(frames) < 8:
                try:
                    frame = desktop.capture_png(max_width=width)
                    frame_dir.mkdir(parents=True, exist_ok=True)
                    path = frame_dir / f"step-{int(started * 1000)}-{index}.png"
                    path.write_bytes(frame["png"])
                    frames.append(str(path))
                except desktop.DesktopControlError:
                    pass

        observation: Dict[str, Any] = {}
        try:
            from core import system_monitor
            if float(watch_sec or 0) > 0:
                observation = system_monitor.watch_processes(
                    duration_sec=min(30.0, float(watch_sec)), interval_sec=1.0, top=3)
            else:
                observation = system_monitor.system_snapshot(top=5)
        except Exception as exc:  # noqa: BLE001 - метрики не должны ломать управление
            observation = {"error": f"метрики недоступны: {exc}"}

        summary = {
            "steps": report,
            "elapsed_sec": round(time.monotonic() - started, 2),
            "frames": frames,
            "screen": desktop.screen_rect(),
            "cursor": list(desktop.cursor_position()),
            "observation": observation,
        }
        if not screenshot:
            return json.dumps(summary, ensure_ascii=False)
        try:
            frame = desktop.capture_png(max_width=width)
        except desktop.DesktopControlError as exc:
            summary["screenshot_error"] = str(exc)
            return json.dumps(summary, ensure_ascii=False)
        summary["screenshot"] = {"width": frame["width"], "height": frame["height"],
                                 "scale": frame["scale"]}
        return {
            "__image__": base64.b64encode(frame["png"]).decode("ascii"),
            "media_type": "image/png",
            "description": json.dumps(summary, ensure_ascii=False),
        }

    def desktop_state(self) -> str:
        """Discover windows before choosing a foreground target for GUI control."""
        from core import desktop_control as desktop

        if not desktop.IS_WINDOWS:
            raise ToolError("Список окон доступен только в Windows.")
        return json.dumps({
            "screen": desktop.screen_rect(),
            "cursor": list(desktop.cursor_position()),
            "foreground": desktop.foreground_window(),
            "windows": desktop.list_windows(limit=30),
        }, ensure_ascii=False)

    def process_monitor(self, action: str, pattern: str = "", top: int = 10,
                        duration_sec: float = 3.0, interval_sec: float = 1.0) -> str:
        """Снимок метрик, поиск процессов или наблюдение в реальном времени."""
        from core import system_monitor

        kind = str(action or "snapshot").strip().lower()
        if kind == "snapshot":
            data = system_monitor.system_snapshot(top=int(top or 10))
        elif kind == "find":
            data = system_monitor.find_processes(pattern or None, top=int(top or 10))
        elif kind == "watch":
            data = system_monitor.watch_processes(duration_sec=duration_sec,
                                                 interval_sec=interval_sec,
                                                 top=int(top or 5),
                                                 pattern=pattern or None)
        else:
            raise ToolError("action должен быть snapshot, find или watch.")
        return json.dumps(data, ensure_ascii=False)

    def open_application(self, program: str, args: str = "") -> str:
        if not self.allow_commands:
            raise ToolError("Запуск приложений отключён в настройках агента.")
        program = str(program or "").strip()
        if not program:
            raise ToolError("Не указано приложение.")
        self._ask("open_application", f"{program} {args}".strip(),
                  "Запустить приложение")
        command = program if not args else f'{program} {args}'
        try:
            # Console children of a windowed app need CREATE_NO_WINDOW anyway;
            # the extra process group makes GUI launchers detachable on Windows.
            extra = {}
            if os.name == "nt":
                extra["creationflags"] = CREATE_NO_WINDOW | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            popen_hidden(command, cwd=str(self.root or os.getcwd()), shell=True, **extra)
        except OSError as exc:
            raise ToolError(f"Не удалось запустить: {exc}") from exc
        return f"Приложение запущено: {command}"

    def _explicit_approval(self, name, title, details):
        if self.access_mode == "full":
            return
        if self.confirm is None or not self.confirm(name, title, details):
            raise ToolError("Пользователь отклонил действие.")

    def capture_screen(self):
        if self.ui_action is None:
            raise ToolError("Снимок экрана доступен только в окне CodeLane.")
        self._explicit_approval("capture_screen", "Сделать снимок экрана?",
                                "Агент получит изображение всего экрана, включая другие приложения.")
        return json.loads(self.ui_action("capture_screen", {}))

    def screenshot(self, path: Optional[str] = None,
                   region: Optional[Dict[str, Any]] = None,
                   max_width: Optional[int] = None) -> str:
        """Сделать снимок экрана и сохранить его в PNG-файл."""
        self._explicit_approval("screenshot", "Сделать снимок экрана?",
                                "PNG-файл сохранится в проекте.")
        from core import desktop_control as desktop

        if not desktop.IS_WINDOWS:
            raise ToolError("Снимок экрана реализован для Windows.")
        if max_width is not None:
            max_width = max(320, min(1920, int(max_width)))
        if path:
            target = self._resolve(path)
        else:
            target = Path(self.root) if self.root else Path.cwd()
            target = target / "screenshoots"
            target.mkdir(parents=True, exist_ok=True)
            target = target / ("screen-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            info = desktop.save_screenshot(path=str(target), region=region,
                                           max_width=max_width)
        except (desktop.DesktopControlError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(info, ensure_ascii=False)

    def desktop_stream(self, duration_sec: float = 3.0,
                       interval_sec: float = 0.5,
                       max_width: int = 1280,
                       save_screenshots: bool = True,
                       top: int = 10) -> Any:
        """Параллельный просмотр экрана и диспетчера задач в реальном времени."""
        self._explicit_approval("desktop_stream", "Живой просмотр экрана?",
                                "Кадры экрана и метрики процессов снимаются одновременно.")
        from core import desktop_control as desktop
        from core import system_monitor

        if not desktop.IS_WINDOWS:
            raise ToolError("Живой просмотр экрана реализован для Windows.")
        duration_sec = min(30.0, max(0.2, float(duration_sec)))
        interval_sec = min(5.0, max(0.1, float(interval_sec)))
        max_width = min(1920, max(320, int(max_width)))
        top = min(50, max(1, int(top)))
        save_dir = None
        if save_screenshots:
            base = Path(self.root) if self.root else Path.cwd()
            save_dir = base / "screenshoots"
            save_dir.mkdir(parents=True, exist_ok=True)
            save_dir = str(save_dir)

        screen_result: Dict[str, Any] = {}
        proc_result: Dict[str, Any] = {}
        errors: List[str] = []

        def _screen():
            try:
                screen_result.update(desktop.watch_screen(
                    duration_sec=duration_sec, interval_sec=interval_sec,
                    max_width=max_width, save_dir=save_dir))
            except Exception as exc:  # noqa: BLE001 - ошибку покажем в отчёте
                errors.append("screen: %s" % exc)

        def _proc():
            try:
                proc_result.update(system_monitor.watch_processes(
                    duration_sec=duration_sec, interval_sec=interval_sec,
                    top=top))
            except Exception as exc:  # noqa: BLE001 - ошибку покажем в отчёте
                errors.append("processes: %s" % exc)

        screen_thread = threading.Thread(target=_screen)
        proc_thread = threading.Thread(target=_proc)
        screen_thread.start()
        proc_thread.start()
        screen_thread.join()
        proc_thread.join()

        summary = {
            "screen": {k: v for k, v in screen_result.items() if k != "last_png"},
            "processes": proc_result,
            "errors": errors,
        }
        last_png = screen_result.get("last_png")
        if not last_png:
            return json.dumps(summary, ensure_ascii=False)
        return {
            "__image__": base64.b64encode(last_png).decode("ascii"),
            "media_type": "image/png",
            "description": json.dumps(summary, ensure_ascii=False),
        }

    def run_admin_command(self, command: str, shell: str = "cmd"):
        if os.name != "nt":
            raise ToolError("Повышение прав через UAC поддерживается только в Windows.")
        command = str(command or "").strip()
        if not command:
            raise ToolError("Пустая команда.")
        shell = str(shell or "cmd").lower()
        if shell not in {"cmd", "powershell"}:
            raise ToolError("Укажи оболочку cmd или powershell.")
        if self.access_mode != "full":
            raise ToolError("Запуск с повышением доступен только в режиме полного доступа.")
        self._explicit_approval("run_admin_command", "Запустить от имени администратора?", command)
        import ctypes
        if shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if executable is None:
                raise ToolError("PowerShell не найден на компьютере.")
            arguments = subprocess.list2cmdline(["-NoProfile", "-Command", command])
        else:
            executable, arguments = "cmd.exe", "/c " + command
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable,
                                                       arguments, str(self.root or Path.cwd()), 0)
        if result <= 32:
            raise ToolError(f"Команда не запущена (UAC или ShellExecute: {result}).")
        return "Команда администратора запущена. Вывод и код завершения недоступны в CodeLane."

    def run_executable(self, path: str, args: str = ""):
        target = self._resolve(path)
        if not target.is_file() or (os.name == "nt" and target.suffix.lower() != ".exe"):
            raise ToolError("Укажите исполняемый файл (.exe).")
        self._ask("run_executable", f"{target} {args}".strip(), "Запустить исполняемый файл")
        arguments = [str(target), *shlex.split(str(args or ""), posix=os.name != "nt")]
        process = popen_hidden(arguments, cwd=str(self.root or target.parent), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        return f"Исполняемый файл запущен: {target.name}, PID {process.pid}."

    def _project_python(self) -> str:
        """Interpreter for project tooling: project virtualenv first, then system Python."""
        from utils.toolchain_detector import python_interpreter
        executable = None
        if self.root is not None:
            executable = next((str(path) for path in (
                self.root / ".venv" / "Scripts" / "python.exe",
                self.root / "venv" / "Scripts" / "python.exe",
                self.root / ".venv" / "bin" / "python",
            ) if path.is_file()), None)
        return python_interpreter(executable)

    @staticmethod
    def _split_tool_args(value: str) -> List[str]:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except ValueError as exc:
            raise ToolError(f"Не удалось разобрать аргументы: {exc}") from exc

    def _run_project_command(self, arguments: List[str], timeout_sec: int) -> str:
        """Run a toolchain command in the project with user confirmation and output capture."""
        if not self.allow_commands:
            raise ToolError("Выполнение команд отключено в настройках агента.")
        if type(timeout_sec) is not int or not 1 <= timeout_sec <= MAX_TIMEOUT:
            raise ToolError(f"timeout_sec должен быть от 1 до {MAX_TIMEOUT}.")
        command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
        return self.run_terminal_command(command, timeout_sec=timeout_sec)

    def run_project_tests(self, target: str = "", timeout_sec: int = DEFAULT_TIMEOUT):
        if self.root is None:
            raise ToolError("Проект не открыт.")
        requested = [str(self._resolve(target))] if target else []
        if any((self.root / name).exists() for name in ("pytest.ini", "pyproject.toml", "tests")):
            arguments = [self._project_python(), "-m", "pytest", "-q", *requested]
        elif (self.root / "package.json").is_file():
            if requested:
                raise ToolError("Выбор файла теста поддерживается только для pytest.")
            arguments = ["npm", "test"]
        else:
            raise ToolError("Конфигурация тестов проекта не обнаружена.")
        return self._run_project_command(arguments, timeout_sec)

    def python(self, code: str = "", path: str = "", args: str = "",
               timeout_sec: int = DEFAULT_TIMEOUT):
        """Run the project interpreter with ``-c`` code or a script file."""
        if not isinstance(code, str) or not isinstance(path, str) or not isinstance(args, str):
            raise ToolError("code, path и args должны быть строками.")
        if bool(code.strip()) == bool(path.strip()):
            raise ToolError("Укажи либо code (код для -c), либо path (файл .py).")
        arguments = [self._project_python()]
        if code.strip():
            arguments += ["-c", code.strip()]
        else:
            target = self._resolve(path)
            if not target.is_file():
                raise ToolError(f"Скрипт не найден: {path}")
            arguments.append(str(target))
        arguments += self._split_tool_args(args)
        return self._run_project_command(arguments, timeout_sec)

    def pytest(self, target: str = "", args: str = "", timeout_sec: int = DEFAULT_TIMEOUT):
        """Run pytest through the project interpreter."""
        if not isinstance(target, str) or not isinstance(args, str):
            raise ToolError("target и args должны быть строками.")
        if self.root is None:
            raise ToolError("Проект не открыт.")
        arguments = [self._project_python(), "-m", "pytest"]
        if target.strip():
            arguments.append(str(self._resolve(target)))
        extra = self._split_tool_args(args)
        arguments += extra or ["-q"]
        return self._run_project_command(arguments, timeout_sec)

    def pip(self, action: str = "list", packages: str = "", args: str = "",
            timeout_sec: int = DEFAULT_TIMEOUT):
        """Run ``pip`` for the project interpreter (install, list, freeze, ...)."""
        action = str(action or "").strip().lower()
        if action not in {"install", "uninstall", "list", "show", "freeze", "check", "download"}:
            raise ToolError("Действие pip должно быть: install, uninstall, list, show, freeze, check или download.")
        if not isinstance(packages, str) or not isinstance(args, str):
            raise ToolError("packages и args должны быть строками.")
        requested = self._split_tool_args(packages)
        if action in {"install", "uninstall", "show", "download"} and not requested:
            raise ToolError(f"Для pip {action} укажи packages.")
        if action in {"list", "freeze", "check"} and requested:
            raise ToolError(f"pip {action} не принимает packages; добавь параметры в args.")
        arguments = [self._project_python(), "-m", "pip", action, *requested,
                     *self._split_tool_args(args)]
        return self._run_project_command(arguments, timeout_sec)

    @staticmethod
    def _git_executable() -> str:
        found = shutil.which("git") or shutil.which(
            "git", path=refreshed_user_path(os.environ.get("PATH", "")))
        if found is None:
            for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(variable)
                if not base:
                    continue
                candidate = Path(base) / "Git" / "cmd" / "git.exe"
                if candidate.is_file():
                    return str(candidate)
        if found is None:
            raise ToolError("Git не найден. Установите Git или добавьте его в PATH.")
        return found

    GIT_SUBCOMMANDS = frozenset({
        "status", "diff", "log", "show", "blame", "branch", "tag", "remote", "stash",
        "add", "restore", "reset", "checkout", "switch", "commit", "merge", "rebase",
        "cherry-pick", "revert", "rm", "mv", "fetch", "pull", "push", "rev-parse",
        "ls-files", "config", "describe", "shortlog", "clean", "init", "clone",
        "version", "help",
    })

    def git(self, args: str = "", timeout_sec: int = DEFAULT_TIMEOUT):
        """Run a git subcommand inside the open project."""
        if not isinstance(args, str):
            raise ToolError("args должны быть строкой.")
        tokens = self._split_tool_args(args)
        if not tokens:
            raise ToolError("Укажи аргументы git, например \"status --short\".")
        # Global options may precede the subcommand: git --no-pager status.
        subcommand = next((token for token in tokens if not token.startswith("-")), "")
        if subcommand not in self.GIT_SUBCOMMANDS:
            raise ToolError(f"Подкоманда git {subcommand or args} не поддерживается инструментом.")
        arguments = [self._git_executable(), *tokens]
        return self._run_project_command(arguments, timeout_sec)

    # -- dispatch ------------------------------------------------------
    def workspace_state(self):
        if self.ui_action is None:
            raise ToolError("Интерфейс CodeLane недоступен.")
        return self.ui_action("workspace_state", {})

    def browser_navigate(self, url: str):
        if self.ui_action is None:
            raise ToolError("Браузер CodeLane недоступен.")
        return self.ui_action("browser_navigate", {"url": str(url)})

    def analysis_refresh(self):
        if self.ui_action is None:
            raise ToolError("Анализатор CodeLane недоступен.")
        return self.ui_action("analysis_refresh", {})

    def editor_state(self):
        if self.ui_action is None:
            raise ToolError("Редактор недоступен.")
        return self.ui_action("editor_state", {})

    def editor_cursor(self, line: int, column: int = 1):
        if self.ui_action is None:
            raise ToolError("Редактор недоступен.")
        return self.ui_action("editor_cursor", {"line": max(1, int(line)),
                                                "column": max(1, int(column))})

    def run(self, name: str, arguments: str) -> str:
        """Execute one tool call; *arguments* is the raw JSON the model sent."""
        if self.should_stop and self.should_stop():
            raise ToolError("Запрос остановлен.")
        handlers = {
            "find_files": self.find_files,
            "view_image": self.view_image,
            "apply_patch": self.apply_patch,
            "exec_command": self.exec_command,
            "write_stdin": self.write_stdin,
            "workspace_state": self.workspace_state,
            "browser_navigate": self.browser_navigate,
            "analysis_refresh": self.analysis_refresh,
            "editor_state": self.editor_state,
            "editor_cursor": self.editor_cursor,
            "read_file": self.read_file,
            "read_document": self.read_document,
            "open_file": self.open_file,
            "list_directory": self.list_directory,
            "search_in_project": self.search_in_project,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_terminal_command": self.run_terminal_command,
            "open_application": self.open_application,
            "capture_screen": self.capture_screen,
            "screenshot": self.screenshot,
            "desktop_stream": self.desktop_stream,
            "computer_control": self.computer_control,
            "desktop_state": self.desktop_state,
            "process_monitor": self.process_monitor,
            "run_admin_command": self.run_admin_command,
            "run_executable": self.run_executable,
            "run_project_tests": self.run_project_tests,
            "python": self.python,
            "pytest": self.pytest,
            "pip": self.pip,
            "git": self.git,
        }
        handler = handlers.get(str(name))
        if handler is None and name not in WORKBENCH_TOOLS:
            raise ToolError(f"Неизвестный инструмент: {name}")
        if not self._allowed(name):
            raise ToolError(f"Инструмент {name} запрещён настройками доступа агента.")
        try:
            payload = json.loads(arguments) if arguments else {}
        except ValueError as exc:
            raise ToolError(f"Некорректные аргументы инструмента: {exc}") from exc
        if not isinstance(payload, dict):
            raise ToolError("Аргументы инструмента должны быть объектом JSON.")
        try:
            if name in WORKBENCH_TOOLS:
                return self._run_workbench(name, payload)
            return handler(**payload)
        except TypeError as exc:
            raise ToolError(f"Неверные аргументы для {name}: {exc}") from exc
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as tool output
            raise ToolError(f"Ошибка инструмента {name}: {exc}") from exc

    def _run_workbench(self, name, payload):
        access, description, properties, required = WORKBENCH_TOOLS[name]
        if set(payload) - properties.keys() or set(required) - payload.keys():
            raise ToolError(f"Проверь аргументы инструмента {name}.")
        types = {"string": str, "integer": int, "boolean": bool, "array": list}
        for key, value in payload.items():
            schema = properties[key]
            if type(value) is not types[schema["type"]] or (
                "enum" in schema and value not in schema["enum"]
            ) or ("minimum" in schema and value < schema["minimum"]) or (
                "maximum" in schema and value > schema["maximum"]
            ) or (
                "items" in schema and (any(item not in schema["items"]["enum"] for item in value)
                                       or len(set(value)) != len(value))
            ):
                raise ToolError(f"Недопустимое значение аргумента {key}.")
        for key in ("path", "destination"):
            if key in payload:
                payload[key] = str(self._resolve(payload[key]))
        if name == "project_config_edit" and "launch_command" in payload and not self.allow_commands:
            raise ToolError("Изменение команды запуска отключено настройками агента.")
        if name == "workbench_control" and payload["action"] == "save_all" and not self.allow_write:
            raise ToolError("Сохранение файлов отключено настройками агента.")
        if access == "write":
            self._ask(name, json.dumps(payload, ensure_ascii=False), description)
        if name == "terminal_execute":
            self._ask("run_terminal_command", payload["command"], description)
        if name.startswith(("explorer_", "analysis_", "dependency_", "lsp_", "todo_", "project_config", "git_")) or (
            name in ("terminal_execute", "editor_save", "test_results") or "path" in payload
        ):
            payload["_project_root"] = str(self.root) if self.root else None
        return self.ui_action(name, payload)
