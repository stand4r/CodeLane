"""PyScribe project lifecycle and workspace handler."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PyQt6 import QtCore
from PyQt6.QtWidgets import QDialog, QMessageBox

from core.activity_log import record_action
from core.logging_config import get_logger
from core.project_state import project_state_file, sync_project_config
from utils.programs import load_session_state
from widgets.ProjectDialog import ProjectDialog

if TYPE_CHECKING:
    from app.main_window import ModernMainWindow

logger = get_logger("project_handler")


class ProjectHandler:
    def __init__(self, main_window: 'ModernMainWindow'):
        self.main_window = main_window

    def open_project(self):
        dialog = ProjectDialog(self.main_window)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted and dialog.selected_project is not None:
            self.on_project_opened(dialog.selected_project)

    def apply_analyzers(self, project_config, selected):
        from widgets.ProjectManager import project_manager

        current = project_config.analyzers
        project_config.analyzers = list(selected)
        try:
            project_manager.update_project_config(project_config)
        except OSError as exc:
            project_config.analyzers = current
            QMessageBox.warning(self.main_window, "Не удалось сохранить анализаторы", str(exc))
            return False
        self.main_window.intelligence_panel.set_project(project_config.root_path, selected)
        return True

    def _normalize_root(self, project_config) -> str:
        if hasattr(project_config, "root_path"):
            return str(Path(project_config.root_path).resolve())
        if isinstance(project_config, dict):
            return str(Path(project_config.get("root", project_config.get("root_path", "."))).resolve())
        if isinstance(project_config, (str, Path)):
            return str(Path(project_config).resolve())
        return ""

    def on_project_opened(self, project_config):
        """Open a project with its saved analyzer profile."""
        norm_root = self._normalize_root(project_config)
        record_action("project_open_requested")
        if not norm_root:
            return

        if hasattr(project_config, "root_path"):
            project_config.root_path = norm_root

        sync_project_config(project_config, norm_root)
        enabled = getattr(self.main_window, "extension_enabled", lambda key: True)
        analyze = enabled("intelligence")
        analyzers = getattr(project_config, 'analyzers', None)
        if analyzers is None and isinstance(project_config, dict):
            analyzers = project_config.get('analyzers')
        if analyzers is None:
            analyzers = ['structure', 'dependencies', 'health']

        if self.main_window.close_all_editor_tabs() is False:
            from widgets.ProjectManager import project_manager
            project_manager.current_project = self.main_window.current_project
            return
        self.main_window.close_special_tab('welcome')
        self.main_window.close_special_tab('settings')

        self.main_window.current_project = project_config
        self.main_window._project_session_restoring = False
        project_state_file(norm_root, "tasks.json", default={"version": 1, "tasks": []})
        project_state_file(norm_root, "ai_chat.json", default={"version": 1, "messages": []})
        # Build the completion index in small batches instead of parsing the
        # whole project on the UI thread (that froze the window on big trees).
        # Even the file-list walk is deferred: it scans thousands of paths and
        # used to hold the GUI thread while the project view was appearing.
        def start_index():
            if self.main_window.current_project is project_config:
                self._start_index(norm_root)

        QtCore.QTimer.singleShot(30, start_index)
        name = getattr(project_config, "name", Path(norm_root).name)
        self.main_window._analysis_failed = False
        # Show the workspace first. Analysis, language servers and the task
        # board scan the tree and used to freeze the window before it appeared.
        def start_project_services():
            if self.main_window.current_project is not project_config:
                return
            if analyze:
                self.main_window.start_busy_indicator(f"Анализ проекта '{name}'...")
                try:
                    self.main_window.intelligence_panel.set_project(norm_root, analyzers)
                except Exception:
                    logger.exception("PyScribe operation failed")
            self.main_window.extension_manager.set_project_root(
                norm_root, confirm=self._confirm_project_extensions)
            try:
                from core.code_analysis.service import UnifiedCodeAnalysisService
                UnifiedCodeAnalysisService.get_instance().set_project_root(norm_root)
            except Exception:
                pass
            try:
                if hasattr(self.main_window, "todo_panel") and self.main_window.todo_panel:
                    self.main_window.todo_panel.set_project(norm_root)
            except Exception:
                pass

        QtCore.QTimer.singleShot(0, start_project_services)
        self.main_window.refresh_extensions_panel()
        self.main_window.search_panel.set_root(norm_root)
        database = getattr(self.main_window, "database_manager", None)
        if database is not None:
            database.set_project(norm_root)
        self.main_window.git_panel.set_root(norm_root if enabled("git") else None)
        self.main_window.update_window_title()

        self.main_window.project_indicator.setText(name)
        self.main_window.project_indicator.setVisible(True)
        self.main_window._set_explorer_location_title(norm_root)

        self._init_workspace_dir(norm_root)
        self._update_file_explorer_root(norm_root)

        if hasattr(self.main_window, 'activity_bar'):
            self.main_window.activity_bar.set_activity("explorer")
        if not self.main_window.sidebar_visible:
            self.main_window.toggle_sidebar()

        self._restore_project_session_files(norm_root, project_config)
        self.main_window._update_project_menu()
        self.main_window.refresh_editor_run_controls()
        self.main_window.status_bar.showMessage(f'Проект открыт: {name} · рабочая область')
        self.main_window.update_analysis_panels()
        self._sync_ai_context()

    def _sync_ai_context(self):
        """Project context is selected automatically once a project is open."""
        panel = getattr(self.main_window, "ai_panel", None)
        if panel is None:
            return
        try:
            panel.sync_scope_with_project()
        except Exception:
            logger.exception("Не удалось синхронизировать контекст ИИ-агента")

    def _set_terminal_workspace(self, root_str: str):
        if not self.main_window.console_widget:
            return
        terminal = self.main_window.console_widget.get_terminal()
        if terminal:
            terminal.switch_session(root_str)

    def _update_file_explorer_root(self, norm_root: str):
        fe = getattr(self.main_window, 'file_explorer', None)
        if not fe or not fe.model:
            return
        try:
            root_index = fe.model.setRootPath(norm_root)
            fe.set_workspace_root(norm_root)
            fe.setRootIndex(root_index)
        except Exception:
            logger.exception("PyScribe operation failed")

    def _start_index(self, norm_root: str):
        """Walk the project tree for completion, then feed it in batches.

        The walk is a pure file-system job, so it runs on the thread pool and
        the GUI thread only receives the finished list; the parsing batches
        then run from a timer.
        """
        index = getattr(self.main_window, 'workspace_index', None)
        if index is None:
            return
        try:
            index.prepare_root(norm_root)
            from services.background import run_async
            project = self.main_window.current_project

            def index_ready(files):
                if self.main_window.current_project is project:
                    self._on_index_files_scanned(files)

            run_async(
                index.scan_source_files,
                on_done=index_ready,
                on_error=lambda message: logger.warning(
                    "Не удалось просканировать проект для индекса: %s", message),
            )
        except Exception:
            logger.exception("Не удалось построить индекс проекта")

    def _on_index_files_scanned(self, files):
        """Completion index receives the discovered files (GUI thread)."""
        try:
            self.main_window.workspace_index.set_pending_files(files or [])
            self.main_window.start_index_batching()
        except Exception:
            logger.exception("Не удалось построить индекс проекта")

    def _restore_project_session_files(self, norm_root: str, project_config: dict):
        """Reopen the project's tabs one per event-loop turn.

        Opening every restored file in one go blocked the GUI thread for the
        whole batch: each editor builds a syntax highlighter for its text, and
        while that runs every animation in the window stops. Spreading the work
        keeps the workbench painting between files.
        """
        state = load_session_state(project_path=norm_root)
        if not state:
            self.open_project_files(project_config)
            self.main_window.save_current_session()
            return
        files = iter(state["files"])
        project = self.main_window.current_project
        self._restore_token = token = object()
        self.main_window._project_session_restoring = True

        def restore_next():
            if token is not self._restore_token or self.main_window.current_project is not project:
                return
            path = next(files, None)
            if path is None:
                self.main_window._project_session_restoring = False
                self.main_window.activate_session_file(state.get("active"))
                return
            self.main_window.restore_session_file(path, state.get("cursors"))
            QtCore.QTimer.singleShot(24, restore_next)

        QtCore.QTimer.singleShot(0, restore_next)

    def _init_workspace_dir(self, norm_root: str):
        project_root = Path(norm_root)
        try:
            project_root.mkdir(parents=True, exist_ok=True)
            project_root_str = str(project_root)
            self.main_window.workspace_path = project_root_str
            self._set_terminal_workspace(project_root_str)
        except Exception as exc:
            self.main_window.workspace_path = norm_root
            if hasattr(self.main_window, 'workbench'):
                self.main_window.workbench.announce(f'Workspace fallback: {exc}')

    def _find_entry_document(self, project_path: Path):
        readme_files = sorted(project_path.glob("README*"))
        if readme_files:
            return str(readme_files[0])
        for cand in ["main.py", "app.py", "index.js", "index.html", "src/main.rs", "main.go"]:
            p = project_path / cand
            if p.is_file():
                return str(p)
        return None

    def _confirm_project_extensions(self, root):
        buttons = QMessageBox.StandardButton
        return QMessageBox.question(
            self.main_window, "Доверие к расширениям проекта",
            f"В папке {root} есть Python-расширения. Разрешить выполнить их код для текущего сеанса проекта?",
            buttons.Yes | buttons.No, buttons.No,
        ) == buttons.Yes

    def open_project_files(self, project_config):
        """Open a useful entry document (README, main.py, index.js, etc.)"""
        try:
            root = self._normalize_root(project_config)
            if not root:
                return
            entry = self._find_entry_document(Path(root))
            if entry:
                self.main_window.file_manager.open_file(entry)
        except Exception:
            logger.exception("PyScribe operation failed")

    def run_project(self):
        if not self.main_window.current_project:
            self.show_no_project_message()
            return

        project = self.main_window.current_project

        if not project.launch_command:
            reply = QMessageBox.question(
                self.main_window, "Конфигурация проекта",
                "Для этого проекта не настроена команда запуска. Настроить её сейчас?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )

            if reply == QMessageBox.StandardButton.Yes:
                self.main_window.action_project_settings()
            return

        if self.main_window.terminal_widget:
            self.main_window.run_in_terminal(
                project.launch_command, 
                working_dir=project.root_path
            )

    def show_no_project_message(self):
        QMessageBox.information(
            self.main_window, 
            "Нет открытого проекта", 
            "Сейчас нет открытого проекта.\n\n"
            "Используйте «Открыть проект», чтобы открыть существующий проект,\n"
            "или «Управление проектами», чтобы создать новый."
        )
