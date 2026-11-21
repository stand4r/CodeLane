from pathlib import Path

from PyQt6 import QtWidgets

from core.assembly import EXTENSIONS as ASSEMBLY_EXTENSIONS
from design.icons import set_line_icon


RUNNABLE_SUFFIXES = ASSEMBLY_EXTENSIONS | {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bash", ".zsh",
    ".go", ".rs", ".c", ".cpp", ".cc", ".cxx", ".java", ".cs", ".php",
    ".rb", ".kt", ".swift", ".lua",
}


def is_runnable_file(path):
    return bool(path) and Path(path).suffix.lower() in RUNNABLE_SUFFIXES


class EditorRunControls(QtWidgets.QWidget):
    def __init__(self, window, group, parent):
        super().__init__(parent)
        self.window = window
        self.group = group
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.play = QtWidgets.QToolButton(self)
        self.play.setObjectName("editorTabAction")
        self.play.setAccessibleName("Запустить файл")
        self.play.clicked.connect(self._run_file)
        layout.addWidget(self.play)

        self.dropdown = QtWidgets.QToolButton(self)
        self.dropdown.setObjectName("editorRunDropdown")
        self.dropdown.setAccessibleName("Варианты запуска")
        self.dropdown.setToolTip("Варианты запуска")
        self.dropdown.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        layout.addWidget(self.dropdown)

        menu = QtWidgets.QMenu(self.dropdown)
        self.file_action = menu.addAction("Запустить файл", self._run_file)
        self.project_action = menu.addAction("Запустить проект", self._run_project)
        self.analyze_separator = menu.addSeparator()
        self.analyze_action = menu.addAction("Запуск и анализ", self._run_and_analyze)
        menu.aboutToShow.connect(self.refresh)
        self.dropdown.setMenu(menu)
        self.on_ui_scale_changed()
        self.refresh()

    def on_ui_scale_changed(self, scale=None):
        from app.ui_scale import scale_px

        self.play.setFixedSize(scale_px(29), scale_px(29))
        self.dropdown.setFixedSize(scale_px(20), scale_px(29))
        set_line_icon(self.play, "play", scale_px(18))
        set_line_icon(self.dropdown, "chevron-down", scale_px(13))

    def _file_path(self):
        tab = self.group.currentWidget()
        path = getattr(tab, "file_path", None)
        if hasattr(tab, "get_code") and hasattr(tab, "set_code") and is_runnable_file(path):
            return path
        return None

    def refresh(self):
        path = self._file_path()
        has_project = bool(self.window.current_project)
        self.setVisible(bool(path) or has_project)
        self.play.setVisible(bool(path))
        if path:
            self.play.setToolTip(f"Запустить {Path(path).name} (F5)")
        self.file_action.setEnabled(bool(path))
        self.project_action.setEnabled(has_project)
        can_analyze = bool(path) and has_project
        self.analyze_separator.setVisible(can_analyze)
        self.analyze_action.setVisible(can_analyze)

    def _run_file(self):
        if self._file_path():
            self.window._set_active_group(self.group)
            self.window.action_run_file()

    def _run_project(self):
        if self.window.current_project:
            self.window._set_active_group(self.group)
            self.window.action_run_project()

    def _run_and_analyze(self):
        path = self._file_path()
        if path and self.window.current_project:
            self.window._set_active_group(self.group)
            self.window.run_and_analyze(path)
