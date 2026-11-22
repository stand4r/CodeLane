"""PyScribe code execution dispatcher."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

from core.assembly import EXTENSIONS as ASSEMBLY_EXTENSIONS
from core.logging_config import get_logger
from utils.settings_manager import get_settings_manager
from utils.toolchain_detector import build_run_command_for_file, python_interpreter, resolve_toolchain
from widgets.Dialog import CustomDialog

if TYPE_CHECKING:
    from app.main_window import ModernMainWindow


logger = get_logger("code_runner")

_SHARED_COMMAND_SUFFIXES = {".ts", ".c", ".cpp", ".cc", ".cxx", ".cs", ".rs", ".java"}
_SIMPLE_TOOLCHAINS = {
    ".js": ("javascript", "node"),
    ".mjs": ("javascript", "node"),
    ".cjs": ("javascript", "node"),
    ".sh": ("shell", "bash"),
    ".bash": ("shell", "bash"),
    ".zsh": ("shell", "bash"),
    ".go": ("go", "go"),
    ".php": ("php", "php"),
    ".rb": ("ruby", "ruby"),
    ".lua": ("lua", "lua"),
}
_SHELL_SUFFIXES = {".h", ".hpp", ".kt", ".swift"}
_PYTHON_SUFFIXES = {".py", ".pyw"}
_TARGET_SUFFIXES = _SHARED_COMMAND_SUFFIXES | _SIMPLE_TOOLCHAINS.keys() | _SHELL_SUFFIXES | _PYTHON_SUFFIXES


def _quote(value):
    return '"' + str(value).replace('"', '') + '"'


class CodeRunner:
    def __init__(self, main_window: 'ModernMainWindow'):
        self.main_window = main_window

    def run_code(self):
        current_editor = self.main_window.get_current_editor()
        if not current_editor:
            CustomDialog("Нет активного редактора").exec()
            return

        if hasattr(current_editor, 'save_file'):
            if not current_editor.save_file():
                CustomDialog("Не удалось сохранить файл перед выполнением").exec()
                return

        file_path = getattr(current_editor, 'file_path', None)
        if not file_path:
            CustomDialog("Нет связанного файла для текущей вкладки").exec()
            return

        args = self.main_window.load_launch_arguments(str(file_path))

        working_dir = None
        if hasattr(self.main_window, 'current_project') and self.main_window.current_project:
            working_dir = self.main_window.current_project.root_path

        self.main_window.run_in_terminal(str(file_path), args, working_dir)

    def run_target(self, file_path, debug=False):
        window = self.main_window
        path = Path(str(file_path)).resolve()
        if not path.exists():
            CustomDialog(f"Файл не найден: {path}").exec()
            return

        editor = window.get_current_editor()
        suffix = path.suffix.lower()
        if suffix == ".sql":
            self._stage_sql(path, editor)
            return

        if editor and str(getattr(editor, "file_path", "")) == str(path) and hasattr(editor, "save_file"):
            if not editor.save_file():
                CustomDialog("Не удалось сохранить файл перед выполнением").exec()
                return

        args = window.load_launch_arguments(str(path))
        project_root = getattr(window.current_project, "root_path", None)
        cwd = Path(project_root) if project_root and Path(project_root).is_dir() else path.parent
        if suffix in ASSEMBLY_EXTENSIONS:
            if debug:
                window.status_bar.showMessage("Для ассемблера доступна команда «Запустить файл».")
            else:
                window.run_in_terminal(str(path), args, cwd)
            return

        if suffix not in _TARGET_SUFFIXES:
            try:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            except Exception:
                logger.exception("Не удалось открыть файл")
            window.status_bar.showMessage(f"Файл открыт системным приложением: {path.name}")
            return

        toolchains = get_settings_manager().get_setting("toolchains", {}) or {}
        try:
            command = self._build_target_command(path, (args or "").strip(), debug, toolchains)
        except RuntimeError as exc:
            CustomDialog(str(exc)).exec()
            return

        if suffix in _SHELL_SUFFIXES:
            window.run_in_terminal_command(command, cwd)
        else:
            if not window.terminal_visible:
                window.toggle_terminal()
            window.console_widget.set_current_tab("Terminal")
            window.console_widget.get_terminal().run_file(command, cwd)
        window.status_bar.showMessage(("Отладка " if debug else "Запуск ") + path.name)

    def _stage_sql(self, path, editor):
        if editor and Path(getattr(editor, "file_path", "") or "").resolve() == path:
            cursor = editor.editor.textCursor()
            source = cursor.selectedText().replace("\u2029", "\n") if cursor.hasSelection() else editor.get_code()
        else:
            try:
                source = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                CustomDialog(str(exc)).exec()
                return
        self.main_window._stage_database_sql(source, path)

    @staticmethod
    def _build_target_command(path, args, debug, toolchains):
        suffix = path.suffix.lower()
        source = _quote(path)
        if suffix in _SHARED_COMMAND_SUFFIXES:
            command, error = build_run_command_for_file(str(path), args, toolchains)
            if command is None:
                raise RuntimeError(error or f"Не удалось подготовить команду запуска: {path.name}")
            return command

        if suffix in _PYTHON_SUFFIXES:
            executable = python_interpreter(resolve_toolchain("python", None, toolchains))
            options = " -m pdb" if debug else ""
            return f"{_quote(executable)}{options} {source} {args}".strip()

        if suffix in _SIMPLE_TOOLCHAINS:
            key, fallback = _SIMPLE_TOOLCHAINS[suffix]
            executable = _quote(resolve_toolchain(key, fallback, toolchains))
            if key == "javascript" and debug:
                executable += " --inspect-brk"
            elif key == "go":
                executable += " run"
            return f"{executable} {source} {args}".strip()

        if suffix in {".h", ".hpp"}:
            key, fallback, flags = ("cpp", "g++", " -std=c++17") if suffix == ".hpp" else ("c", "gcc", "")
            executable = _quote(resolve_toolchain(key, fallback, toolchains))
            output = _quote(path.with_suffix(".pyscribe_run.exe" if os.name == "nt" else ".pyscribe_run"))
            return f"{executable} {source}{flags} -o {output} && {output} {args}".strip()

        if suffix == ".kt":
            jar = _quote(path.with_suffix(".jar"))
            return f"kotlinc {source} -include-runtime -d {jar} && java -jar {jar}"

        return f"swift {source} {args}".strip()
