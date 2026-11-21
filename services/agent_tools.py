"""Local tools the AI agent can call: files, terminal, running applications.

The agent runs model-requested actions through this module instead of touching
the disk directly. Everything is confined to the open project and every
mutating action goes through a confirmation callback, so the user stays in
control (same model as DeepSeek Harness).

Plain Python on purpose — no Qt imports, fully unit-testable.
"""
from __future__ import annotations

import json
from io import StringIO
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.subprocess_utils import CREATE_NO_WINDOW, popen_hidden, run_hidden
from core.file_snapshot import snapshot_bytes, snapshot_file, write_if_unchanged
from services.workbench_tools import TOOLS as WORKBENCH_TOOLS

__all__ = ["ToolError", "LocalToolbox", "TOOL_DEFINITIONS"]

MAX_READ_CHARS = 120_000
MAX_READ_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_CHARS = 2_000_000     # safety valve against a runaway command only
MAX_SEARCH_RESULTS = 500
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 900
MAX_WRITE_BYTES = 8 * 1024 * 1024

ACCESS_MODES = {
    "read_only": "Только чтение",
    "write_only": "Только запись (с подтверждением)",
    "full": "Полный доступ (без подтверждений)",
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
    "read_file": False,
    "list_directory": False,
    "search_in_project": False,
    "write_file": True,
    "run_terminal_command": True,
    "open_application": True,
    "edit_file": True,
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
        "Прочитать текстовый файл проекта. Путь указывается относительно корня проекта. "
        "Большой файл можно читать частями: offset (первая строка, с 1) и limit (сколько строк).",
        {"path": {"type": "string", "description": "Путь к файлу относительно корня проекта"},
         "offset": {"type": "integer",
                    "description": "С какой строки начинать (1 — начало файла)"},
         "limit": {"type": "integer",
                   "description": "Сколько строк вернуть (0 — до конца файла)"}},
        ["path"],
    ),
    _tool(
        "open_file",
        "Открыть файл проекта в редакторе CodeLane и сделать его активной вкладкой.",
        {"path": {"type": "string", "description": "Путь к файлу относительно корня проекта"}},
        ["path"],
    ),
    _tool(
        "list_directory",
        "Показать содержимое каталога проекта (файлы и подкаталоги).",
        {"path": {"type": "string",
                  "description": "Каталог относительно корня; '.' — корень проекта"}},
        ["path"],
    ),
    _tool(
        "search_in_project",
        "Найти текст в файлах проекта; regex=true включает регулярное выражение. "
        "Вернуть совпадения с путями и номерами строк.",
        {"query": {"type": "string", "description": "Искомый текст"},
         "glob": {"type": "string",
                  "description": "Необязательная маска файлов, например '*.py' или '.py'"},
         "regex": {"type": "boolean", "description": "Искать как регулярное выражение"}},
        ["query"],
    ),
    _tool(
        "write_file",
        "Создать файл или полностью заменить его содержимое. Для точечной правки используй edit_file. "
        "Требует подтверждения пользователя.",
        {"path": {"type": "string", "description": "Путь относительно корня проекта"},
         "content": {"type": "string", "description": "Новое содержимое файла"}},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Заменить точный фрагмент в файле проекта. old_text должен встречаться ровно один раз. "
        "Если файл открыт, правка попадёт в редактор и останется несохранённой до обычного сохранения.",
        {"path": {"type": "string", "description": "Путь относительно корня проекта"},
         "old_text": {"type": "string", "description": "Точный исходный фрагмент"},
         "new_text": {"type": "string", "description": "Новый фрагмент"}},
        ["path", "old_text", "new_text"],
    ),
    _tool(
        "run_terminal_command",
        "Выполнить команду в терминале проекта и вернуть вывод и код возврата. "
        "Требует подтверждения пользователя.",
        {"command": {"type": "string", "description": "Команда оболочки"},
         "timeout_sec": {"type": "integer",
                         "description": "Таймаут в секундах (по умолчанию 30)"}},
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
                 should_stop: Optional[Callable[[], bool]] = None):
        self.root = Path(project_root).resolve() if project_root else None
        self.confirm = confirm
        self.allow_write = allow_write
        self.allow_commands = allow_commands
        self.access_mode = normalize_access_mode(access_mode)
        self.ui_action = ui_action
        self.should_stop = should_stop
        self.background = False

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
        if name in ("read_file", "list_directory", "search_in_project", "open_file", "editor_state", "editor_cursor"):
            return self.access_mode != "write_only"
        if name in ("write_file", "edit_file"):
            return self.allow_write and self.access_mode != "read_only"
        if name in ("run_terminal_command", "open_application"):
            return self.allow_commands and self.access_mode == "full"
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
        if self.root is None:
            raise ToolError("Проект не открыт — работа с файлами недоступна.")
        text = str(raw or ".").strip().strip('"').strip("'")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"Не удалось разрешить путь: {exc}") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError("Путь вне проекта: доступ разрешён только внутри проекта.")
        return resolved

    def _ask(self, name: str, details: str, title: str = "") -> None:
        """Ask the user to confirm a mutating action.

        "Полный доступ" means exactly that: the agent works without per-action
        prompts. Only the restricted modes ask (and there is a settings switch
        to disable edits/commands entirely).
        """
        if not _CONFIRM_POLICY.get(name, True):
            return
        if self.access_mode == "full":
            return
        if self.confirm is None:
            raise ToolError("Действие отклонено: подтверждение недоступно.")
        if not self.confirm(name, title or name, details):
            raise ToolError("Пользователь отклонил действие.")

    # -- tools ---------------------------------------------------------
    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        from services.workbench_documents import DOCUMENT_SUFFIXES
        if target.suffix.lower() in DOCUMENT_SUFFIXES:
            raise ToolError("Для PDF, DOCX и ODT используй read_document.")
        text = None
        if self.ui_action is not None:
            try:
                buffer = self.ui_action("read_buffer", {"path": str(target)})
                if buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    text = buffer
            except Exception:
                # The disk copy remains a useful fallback if a UI is closing.
                text = None
        first = max(1, int(offset or 1))
        count = max(0, int(limit or 0))
        collected = []
        remaining = MAX_READ_CHARS
        read = 0
        truncated = False
        try:
            source = (StringIO(text, newline=None) if text is not None else
                      target.open(encoding="utf-8", errors="replace", newline=None))
            with source:
                skipped = 1
                while skipped < first:
                    if self.should_stop and self.should_stop():
                        raise ToolError("Запрос остановлен.")
                    piece = source.readline(65536)
                    if not piece:
                        break
                    if piece.endswith("\n") or len(piece) < 65536:
                        skipped += 1
                if skipped == first:
                    while not count or read < count:
                        line = source.readline(remaining + 1)
                        if not line:
                            break
                        if len(line) > remaining:
                            if read == 0:
                                collected.append(line[:remaining])
                                read += 1
                            truncated = True
                            break
                        collected.append(line)
                        remaining -= len(line)
                        read += 1
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать файл: {exc}") from exc
        answer = "".join(collected)
        if not read:
            return "[Строки в указанном диапазоне отсутствуют.]" if offset or limit else ""
        if offset or limit or truncated:
            header = f"[строки {first}–{first + read - 1}]\n"
            answer = header + answer.rstrip("\n")
        if truncated:
            answer += "\n[Достигнут предел размера ответа.]"
        return answer

    def open_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        if self.background:
            return f"Файл доступен для фоновой работы: {target}. Используй read_file для чтения."
        if self.ui_action is None:
            raise ToolError("Открытие вкладки CodeLane недоступно в текущем режиме.")
        return self.ui_action("open_file", {"path": str(target)})

    def list_directory(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"Каталог не найден: {path}")
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать каталог: {exc}") from exc
        lines = []
        for entry in entries[:2000]:
            if entry.name.startswith(".") and entry.name not in (".pyscribe",):
                continue
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{entry.name}{suffix}")
        if len(entries) > 2000:
            lines.append(f"… (показаны первые 2000 из {len(entries)})")
        if not lines:
            return "(пусто)"
        return "\n".join(lines)

    def search_in_project(self, query: str, glob: str = "", regex: bool = False) -> str:
        if not query:
            raise ToolError("Пустой запрос поиска.")
        if self.root is None:
            raise ToolError("Проект не открыт.")
        from core.ignore_rules import MAX_FILES_DEFAULT, walk_files
        pattern = glob or None
        extension = pattern if pattern and pattern.startswith(".") else None
        scan = walk_files(self.root, extensions={extension} if extension else None,
                          max_files=MAX_FILES_DEFAULT)
        results: List[str] = []
        needle = query.lower()
        try:
            matcher = re.compile(query, re.IGNORECASE) if regex else None
        except re.error as exc:
            raise ToolError(f"Некорректное регулярное выражение: {exc}") from None
        for path in scan.files:
            if extension and path.suffix.lower() != extension.lower():
                continue
            if pattern and not extension and not path.match(pattern):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                matches = matcher.search(line) if matcher is not None else needle in line.lower()
                if matches:
                    relative = path.relative_to(self.root)
                    results.append(f"{relative}:{number}: {line.strip()[:400]}")
                    if len(results) >= MAX_SEARCH_RESULTS:
                        results.append(f"… (показаны первые {MAX_SEARCH_RESULTS} совпадений, "
                                       "уточни запрос или маску файлов)")
                        return "\n".join(results)
        return "\n".join(results) if results else "Совпадений не найдено."

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
        if self.ui_action is not None and existed:
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
        if self.ui_action is not None and not self.background:
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
        if self.ui_action is not None:
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
        return target.relative_to(self.root).as_posix(), current

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
        if self.ui_action is not None:
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

    def run_terminal_command(self, command: str, timeout_sec: int = DEFAULT_TIMEOUT) -> str:
        if not self.allow_commands:
            raise ToolError("Выполнение команд отключено в настройках агента.")
        if self.root is None:
            raise ToolError("Проект не открыт.")
        command = str(command or "").strip()
        if not command:
            raise ToolError("Пустая команда.")
        self._ask("run_terminal_command", f"$ {command}", "Выполнить команду в проекте")
        timeout = max(1, min(int(timeout_sec or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        process = None
        was_stopped = False
        timed_out = False

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
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as capture:
                process = popen_hidden(
                    command, shell=True, cwd=str(self.root),
                    stdout=capture, stderr=subprocess.STDOUT,
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
                output = capture.read(MAX_OUTPUT_CHARS + 1)
        except OSError as exc:
            raise ToolError(f"Не удалось запустить команду: {exc}") from exc
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n… (вывод обрезан)"
        if was_stopped:
            return "Команда остановлена пользователем.\n" + (output.strip() or "(нет вывода)")
        if timed_out:
            return f"Команда не завершилась за {timeout} с и была остановлена.\n{output.strip()}".strip()
        return f"Код возврата: {process.returncode}\n{output.strip() or '(нет вывода)'}"

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
            "workspace_state": self.workspace_state,
            "browser_navigate": self.browser_navigate,
            "analysis_refresh": self.analysis_refresh,
            "editor_state": self.editor_state,
            "editor_cursor": self.editor_cursor,
            "read_file": self.read_file,
            "open_file": self.open_file,
            "list_directory": self.list_directory,
            "search_in_project": self.search_in_project,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_terminal_command": self.run_terminal_command,
            "open_application": self.open_application,
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
                if self.background and name == "terminal_execute":
                    return self.run_terminal_command(payload["command"])
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
        if access == "write":
            self._ask(name, json.dumps(payload, ensure_ascii=False), description)
        if name.startswith(("explorer_", "analysis_", "dependency_", "lsp_", "todo_", "project_config", "git_")) or (
            name in ("terminal_execute", "editor_save") or "path" in payload
        ):
            payload["_project_root"] = str(self.root) if self.root else None
        return self.ui_action(name, payload)
