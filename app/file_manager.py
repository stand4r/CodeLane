"""PyScribe file management subsystem."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from utils.programs import saveRecent, backup
from core.activity_log import record_action
from core.database_workspace import CONNECTION_SUFFIX, FILE_SUFFIXES as DATABASE_FILE_SUFFIXES
from core.file_snapshot import snapshot_bytes
from core.text_file import BYTE_MARKS, TextFileFormat, decode_text_file
from widgets.Dialog import CustomDialog
from widgets.DocumentViewerWidget import DOCUMENT_EXTENSIONS, DocumentViewerWidget
from widgets.HexViewer import HexViewer
from widgets.DiagramViewerWidget import DIAGRAM_EXTENSIONS, DiagramViewerWidget

if TYPE_CHECKING:
    from app.main_window import ModernMainWindow


class FileManager:
    BINARY_EXTENSIONS = {
        ".bin", ".exe", ".out", ".dll", ".so", ".dylib",
        ".elf", ".hex", ".rom", ".img", ".iso", ".dat"
    }

    def __init__(self, main_window: 'ModernMainWindow'):
        self.main_window = main_window
        self.app_manager = main_window.app_manager

    def open_file(self, file_path: str):
        """Open a file using the appropriate editor/viewer."""
        record_action("file_open_requested")
        try:
            file_path_obj = Path(file_path)
            if not file_path_obj.is_file():
                CustomDialog(f"Файл не найден: {file_path}").exec()
                return

            path_str = str(file_path_obj.resolve())
            if self._find_and_focus_open_tab(path_str):
                return

            suffix = file_path_obj.suffix.lower()
            if suffix == CONNECTION_SUFFIX:
                self.main_window.database_manager.open_connection_path(path_str)
                return
            if suffix in DATABASE_FILE_SUFFIXES:
                with file_path_obj.open("rb") as stream:
                    header = stream.read(16)
                if header == b"SQLite format 3\x00" or (not header and suffix in {".sqlite", ".sqlite3"}):
                    self.main_window.database_manager.open_sqlite_path(path_str)
                    return
            if suffix in DIAGRAM_EXTENSIONS:
                self.open_diagram_file(path_str)
                return
            if suffix == ".pdf":
                self.open_pdf_file(path_str)
                return
            if suffix in {".csv", ".tsv", ".tab", ".xlsx", ".xlsm"}:
                self.open_table_file(path_str)
                return
            if suffix in DOCUMENT_EXTENSIONS:
                self.open_document_file(path_str)
                return

            if self._is_binary_file(file_path_obj, suffix):
                self.open_binary_file(path_str)
                return

            content, snapshot, text_format = self._read_text(file_path_obj)
            backup(path_str)
            self.main_window.close_special_tab("welcome")
            self.main_window.create_editor_tab(content, path_str, snapshot=snapshot, text_format=text_format)
            if not self._find_and_focus_open_tab(path_str):
                # create_editor_tab failed (it reports the reason itself);
                # opening the raw text keeps the file reachable.
                self._open_plain_text_fallback(content, path_str)
                return
            self._register_file_open(path_str)

        except Exception as e:
            CustomDialog(f"Ошибка открытия файла: {str(e)}").exec()

    def _open_plain_text_fallback(self, content: str, path_str: str):
        """Last-resort viewer when the rich editor could not be constructed."""
        try:
            from widgets.MarkdownViewerWidget import MarkdownViewerWidget
        except Exception:
            return
        try:
            viewer = MarkdownViewerWidget(file_path=path_str, content=content, parent=self.main_window)
            index = self.main_window.active_tab_widget.addTab(viewer, Path(path_str).name)
            self.main_window.active_tab_widget.setCurrentIndex(index)
        except Exception:
            pass

    def _match_tab_path(self, widget, path_str: str) -> bool:
        fpath = getattr(widget, "file_path", None)
        if not fpath:
            return False
        try:
            return str(Path(fpath).resolve()) == path_str
        except (OSError, TypeError):
            return False

    def _find_tab(self, matches):
        for group in self.main_window.editor_groups():
            for index in range(group.count()):
                widget = group.widget(index)
                if widget is not None and matches(widget):
                    return group, index, widget
        return None

    def _focus_tab(self, tab):
        if tab is None:
            return None
        group, index, widget = tab
        group.setCurrentIndex(index)
        self.main_window._set_active_group(group)
        return widget

    def _find_and_focus_open_tab(self, path_str: str) -> bool:
        tab = self._find_tab(
            lambda widget: not widget.property("pyscribeHtmlPreview")
            and self._match_tab_path(widget, path_str)
        )
        return self._focus_tab(tab) is not None

    def _is_binary_file(self, file_path_obj: Path, suffix: str) -> bool:
        """Check if file should be treated as binary."""
        if suffix in self.BINARY_EXTENSIONS:
            return True
        try:
            with file_path_obj.open("rb") as source:
                sample = source.read(8192)
            if any(sample.startswith(marker) for marker, _ in BYTE_MARKS):
                return False
            return b"\x00" in sample
        except OSError:
            return False

    def _register_file_open(self, path_str: str):
        """Record file into recent history and session files."""
        if path_str not in self.app_manager.session_files:
            self.app_manager.session_files.append(path_str)
        if path_str in self.app_manager.recent_files:
            self.app_manager.recent_files.remove(path_str)
        self.app_manager.recent_files.insert(0, path_str)
        saveRecent(self.app_manager.recent_files[:20])

        if getattr(self.main_window, "recent_files_menu", None) is not None:
            self.main_window.ui_manager._update_recent_files_menu()
        if self.main_window._welcome_tab is not None:
            self.main_window._welcome_tab.set_recent_files(self.app_manager.recent_files)

    @staticmethod
    def _read_text(file_path: Path) -> tuple[str, bytes, TextFileFormat]:
        """Decode source/text files without crashing on BOM or non-UTF-8 files."""
        data = file_path.read_bytes()
        text, text_format = decode_text_file(data)
        return text, snapshot_bytes(data), text_format

    def open_table_file(self, file_path: str):
        """Open CSV/TSV/XLSX in a high-performance table viewer."""
        try:
            from widgets.TableViewerWidget import TableViewerWidget
            path_str = str(Path(file_path).resolve())
            tab = self._find_tab(
                lambda widget: isinstance(widget, TableViewerWidget) and widget.file_path == path_str
            )
            if self._focus_tab(tab) is not None:
                return

            viewer = TableViewerWidget(path_str, self.main_window)
            viewer.open_editor_requested.connect(self.open_text_editor)
            name = Path(file_path).name
            tab_index = self.main_window.active_tab_widget.addTab(viewer, f"Таблица: {name}")
            self.main_window.active_tab_widget.setCurrentIndex(tab_index)
            self._register_file_open(path_str)
            self.app_manager.logger.info("Opened table file: %s", file_path)
        except Exception as e:
            CustomDialog(f"Ошибка открытия таблицы: {str(e)}").exec()

    def open_document_file(self, file_path: str):
        """Open a native Office document tab."""
        viewer = DocumentViewerWidget(file_path, self.main_window)
        viewer.file_path = Path(file_path)
        index = self.main_window.active_tab_widget.addTab(viewer, Path(file_path).name)
        self.main_window.active_tab_widget.setCurrentIndex(index)
        self.app_manager.logger.info("Opened office document: %s", file_path)

    def open_binary_file(self, file_path: str):
        """Open a binary file in a read-only hex editor tab."""
        try:
            path_str = str(Path(file_path).resolve())
            tab = self._find_tab(
                lambda widget: isinstance(widget, HexViewer) and widget.file_path == Path(path_str)
            )
            if self._focus_tab(tab) is not None:
                return

            viewer = HexViewer(self.main_window)
            viewer.file_path = Path(file_path)
            viewer.load_file(Path(file_path).read_bytes())

            name = Path(file_path).name
            tab_index = self.main_window.active_tab_widget.addTab(viewer, f"HEX: {name}")
            self.main_window.active_tab_widget.setCurrentIndex(tab_index)
            self._register_file_open(path_str)
        except Exception as e:
            CustomDialog(f"Ошибка открытия HEX: {str(e)}").exec()

    def open_recent_file(self, file_path: str):
        self.open_file(file_path)
        if file_path in self.app_manager.recent_files:
            self.app_manager.recent_files.remove(file_path)
        self.app_manager.recent_files.insert(0, file_path)
        saveRecent(self.app_manager.recent_files)

    def save_file(self, editor) -> bool:
        if self.main_window.is_special_widget(editor) or isinstance(editor, HexViewer):
            return False
        if not hasattr(editor, 'save_file'):
            return False
        ok = editor.save_file()
        if ok and hasattr(editor, 'editor') and hasattr(editor.editor, 'document'):
            editor.editor.document().setModified(False)
        return ok

    def _save_group_files(self, group):
        for i in range(group.count()):
            editor = group.widget(i)
            if not self.main_window.is_special_widget(editor) and self.save_file(editor):
                self.main_window.remove_unsaved_marker(i, group=group)

    def save_all_files(self):
        for group in self.main_window.editor_groups():
            self._save_group_files(group)
        self.main_window.recalculate_unsaved_count()

    def open_pdf_file(self, file_path: str):
        """Открыть PDF файл в специальном viewer"""
        try:
            from widgets.PDFViewerWidget import PDFViewerWidget

            pdf_viewer = PDFViewerWidget(self.main_window)
            file_name = Path(file_path).name
            tab_index = self.main_window.active_tab_widget.addTab(pdf_viewer, file_name)
            self.main_window.active_tab_widget.setCurrentIndex(tab_index)
            # Load after the tab is current: the viewer needs a real viewport
            # size to calculate fit-width instead of starting at 100%.
            pdf_viewer.open_pdf(file_path)

            if file_path not in self.app_manager.session_files:
                self.app_manager.session_files.append(file_path)

        except Exception as e:
            CustomDialog(f"Ошибка открытия PDF: {str(e)}").exec()

    def open_diagram_file(self, path_str: str):
        viewer = DiagramViewerWidget(path_str, self.main_window)
        viewer.open_editor_requested.connect(self.open_text_editor)
        group = self.main_window.active_tab_widget
        index = group.addTab(viewer, f"Диаграмма: {Path(path_str).name}")
        group.setCurrentIndex(index)
        self._register_file_open(path_str)

    def open_markdown_file(self, file_path: str, source=None):
        """Open Markdown or static HTML with live editor updates."""
        try:
            from widgets.MarkdownViewerWidget import HTML_SUFFIXES, MarkdownViewerWidget
            path_str = str(Path(file_path).resolve())
            is_html = Path(path_str).suffix.lower() in HTML_SUFFIXES
            if source is None and is_html:
                editor_tab = self._find_text_tab(path_str)
                if editor_tab is not None:
                    source = getattr(editor_tab[2], "editor", None)

            tab = self._find_tab(
                lambda widget: isinstance(widget, MarkdownViewerWidget) and widget.file_path == path_str
            )
            viewer = self._focus_tab(tab)
            if viewer is not None:
                if source is not None:
                    viewer.bind_source(source)
                else:
                    viewer.reload()
                return

            viewer = MarkdownViewerWidget(file_path=path_str, parent=self.main_window)
            if is_html:
                viewer.setProperty("pyscribeHtmlPreview", True)
            if source is not None:
                viewer.bind_source(source)
            viewer.open_editor_requested.connect(self.open_text_editor)
            viewer.open_preview_requested.connect(self.open_markdown_file)

            file_name = Path(file_path).name
            tab_index = self.main_window.active_tab_widget.addTab(viewer, f"Просмотр: {file_name}")
            self.main_window.active_tab_widget.setCurrentIndex(tab_index)
            self._register_file_open(path_str)

        except Exception as e:
            CustomDialog(f"Ошибка предпросмотра: {e}").exec()

    def open_text_editor(self, file_path: str, *, activate=True):
        """Force open a file in code/text editor mode."""
        try:
            path_str = str(Path(file_path).resolve())
            existing = self._find_text_tab(path_str)
            if existing is not None:
                if activate:
                    self._focus_tab(existing)
                return existing[2]

            content, snapshot, text_format = self._read_text(Path(file_path))
            if activate:
                self.main_window.close_special_tab("welcome")
            self.main_window.create_editor_tab(content, path_str, snapshot=snapshot,
                                               text_format=text_format, activate=activate)
            existing = self._find_text_tab(path_str)
            return existing[2] if existing is not None else None
        except Exception as e:
            CustomDialog(f"Ошибка открытия в редакторе: {str(e)}").exec()

    def _find_text_tab(self, path_str: str):
        def matches(widget):
            return (
                not isinstance(widget, (HexViewer, DiagramViewerWidget))
                and not hasattr(widget, "browser")
                and callable(getattr(widget, "get_code", None))
                and self._match_tab_path(widget, path_str)
            )

        return self._find_tab(matches)
