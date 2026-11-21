"""PyScribe widgets/SearchPanel.py: application module and reusable component."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from threading import Event, Lock
from design.icons import set_line_icon
from PyQt6 import QtCore, QtWidgets, QtGui


@dataclass
class _ReplacementJob:
    root: Path
    protected: set
    cancel: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    switched_workspace: bool = False


class SearchPanel(QtWidgets.QWidget):
    open_file_requested = QtCore.pyqtSignal(str)
    open_match_requested = QtCore.pyqtSignal(str, int)

    SKIP_DIRS = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'dist', 'build'}
    BINARY_SUFFIXES = {
        '.bin', '.exe', '.out', '.dll', '.so', '.dylib', '.elf', '.hex', '.rom',
        '.img', '.iso', '.dat', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
        '.ico', '.pyc', '.pyd', '.class', '.jar', '.zip', '.7z', '.rar', '.gz',
        '.mp3', '.mp4', '.wav', '.woff', '.woff2', '.ttf', '.otf'
    }
    MAX_FILE_BYTES = 2 * 1024 * 1024

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("searchPanel")
        self.root_path = Path.cwd()
        self._search_cancel = Event()
        self._last_request = None
        self._pending_results = None
        self._replacement = None
        self._build()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('ПОИСК')
        title.setObjectName('sideTitle')
        title.setStyleSheet("font-size: 11px; font-weight: 700; color: #8B949E; letter-spacing: 1px;")
        title_row.addWidget(title)
        title_row.addStretch()
        self.result_count = QtWidgets.QLabel('')
        self.result_count.setStyleSheet('color:#858585; font-size:11px;')
        title_row.addWidget(self.result_count)
        layout.addLayout(title_row)

        self.query = QtWidgets.QLineEdit()
        self.query.setPlaceholderText('Поиск по файлам')
        self.query.setMinimumHeight(32)
        self.query.setClearButtonEnabled(True)
        self.query.setObjectName('searchQuery')
        self.query.returnPressed.connect(self.search)
        layout.addWidget(self.query)

        options = QtWidgets.QHBoxLayout()
        options.setSpacing(6)
        self.case_sensitive = QtWidgets.QToolButton()
        self.case_sensitive.setText('Aa')
        self.case_sensitive.setToolTip('Учитывать регистр (Aa)')
        self.case_sensitive.setCheckable(True)
        self.whole_word = QtWidgets.QToolButton()
        self.whole_word.setText('ab')
        self.whole_word.setToolTip('Слово целиком (ab)')
        self.whole_word.setCheckable(True)
        self.regex = QtWidgets.QToolButton()
        self.regex.setText('.*')
        self.regex.setToolTip('Регулярное выражение (.*)')
        self.regex.setCheckable(True)
        for button in (self.case_sensitive, self.whole_word, self.regex):
            button.setMinimumSize(32, 28)
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Fixed)
            options.addWidget(button)
        options.addStretch()
        self.search_button = QtWidgets.QToolButton()
        self.search_button.setText('Искать')
        set_line_icon(self.search_button, 'search')
        self.search_button.setToolTip('Искать по проекту')
        self.search_button.setMinimumHeight(30)
        self.search_button.setMinimumWidth(72)
        self.search_button.clicked.connect(self.search)
        options.addWidget(self.search_button)
        layout.addLayout(options)

        layout.addSpacing(2)

        self.replace = QtWidgets.QLineEdit()
        self.replace.setPlaceholderText('Заменить на…')
        self.replace.setMinimumHeight(32)
        self.replace.setClearButtonEnabled(True)
        layout.addWidget(self.replace)

        replace_row = QtWidgets.QHBoxLayout()
        replace_row.setContentsMargins(0, 2, 0, 4)
        replace_row.addStretch()
        self.replace_button = QtWidgets.QPushButton('Заменить всё')
        self.replace_button.setMinimumHeight(30)
        self.replace_button.clicked.connect(self.replace_all)
        replace_row.addWidget(self.replace_button)
        self.cancel_replace_button = QtWidgets.QPushButton("Остановить")
        self.cancel_replace_button.clicked.connect(self.stop_replace)
        self.cancel_replace_button.hide()
        replace_row.addWidget(self.cancel_replace_button)
        layout.addLayout(replace_row)

        self.results = QtWidgets.QTreeWidget()
        self.results.setHeaderHidden(True)
        self.results.setRootIsDecorated(True)
        self.results.setUniformRowHeights(True)
        self.results.setIndentation(14)
        self.results.setAnimated(False)
        self.results.itemDoubleClicked.connect(self._open_item)
        self.results.setObjectName("searchResults")
        self.results.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        layout.addWidget(self.results, 1)

    def set_root(self, path):
        root = Path(path) if path else Path.cwd()
        if self._replacement is not None and (path is None or root != self.root_path):
            self._replacement.switched_workspace = True
            self.stop_replace()
        if root != self.root_path:
            self._search_cancel.set()
            self.root_path = root
            self._last_request = None
            self._pending_results = None
            self.results.clear()
            self.result_count.clear()

    def _compile_pattern(self, query):
        if self.regex.isChecked():
            pattern = query
        else:
            pattern = re.escape(query)
            if self.whole_word.isChecked():
                pattern = r'\b' + pattern + r'\b'
        return re.compile(pattern, 0 if self.case_sensitive.isChecked() else re.IGNORECASE)

    @classmethod
    def _iter_text_files(cls, root, cancel=None):
        try:
            from core.ignore_rules import walk_files
            root = Path(root).resolve()
            # Pruned walk so a search never descends into node_modules/.git.
            scan = walk_files(root, extensions=None,
                              max_files=None, max_bytes=cls.MAX_FILE_BYTES,
                              cancelled=cancel.is_set if cancel is not None else None)
            for path in scan.files:
                if cancel is not None and cancel.is_set():
                    return
                if path.suffix.lower() in cls.BINARY_SUFFIXES:
                    continue
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    data = path.read_bytes()
                    # Remember the real encoding so ``replace_all`` can write the
                    # file back without corrupting non-UTF-8 sources.
                    bom = b''
                    encoding = 'utf-8'
                    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
                        bom = data[:2]
                        encoding = 'utf-16-le' if bom == b'\xff\xfe' else 'utf-16-be'
                        text = data[2:].decode(encoding)
                    elif b'\x00' in data[:8192]:
                        continue
                    elif data.startswith(b'\xef\xbb\xbf'):
                        text = data.decode('utf-8-sig')
                        bom = b'\xef\xbb\xbf'
                    else:
                        try:
                            text = data.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data.decode('cp1251')
                            encoding = 'cp1251'
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                yield path, text, encoding, bom
        except OSError:
            return

    def _request(self):
        return (self.query.text(), self.regex.isChecked(), self.whole_word.isChecked(),
                self.case_sensitive.isChecked())

    def refresh_if_needed(self):
        if self._request() != self._last_request:
            self.search()

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_results is not None:
            QtCore.QTimer.singleShot(0, self._render_pending)

    def _render_pending(self):
        if self.isVisible() and self._pending_results is not None:
            result, self._pending_results = self._pending_results, None
            self._show_results(self.root_path, *result)

    def search(self):
        if self._replacement is not None:
            return
        self._search_cancel.set()
        self._last_request = self._request()
        self._pending_results = None
        self.results.clear()
        query = self.query.text()
        if not query.strip():
            self.result_count.clear()
            return
        try:
            pattern = self._compile_pattern(query)
        except re.error as exc:
            self.result_count.setText('Неверное рег. выражение')
            self.results.addTopLevelItem(QtWidgets.QTreeWidgetItem([str(exc)]))
            return

        root = self.root_path
        cancel = self._search_cancel = Event()
        self.result_count.setText('Поиск…')

        def ready(result):
            if not cancel.is_set():
                self._pending_results = result
                self._render_pending()

        def failed(message):
            if not cancel.is_set():
                self.result_count.setText('Ошибка поиска')
                self.results.addTopLevelItem(QtWidgets.QTreeWidgetItem([message]))

        from services.background import run_async
        run_async(lambda: SearchPanel._find_matches(root, pattern, cancel), on_done=ready, on_error=failed)

    @classmethod
    def _find_matches(cls, root, pattern, cancel):
        files = []
        total_matches = 0
        for path, text, _encoding, _bom in cls._iter_text_files(root, cancel):
            if cancel.is_set():
                break
            rows = []
            for line_no, line in enumerate(text.splitlines(), 1):
                count = sum(1 for _ in pattern.finditer(line))
                if count:
                    snippet = line.strip()
                    rows.append((line_no, snippet if len(snippet) <= 180 else snippet[:177] + '…'))
                    total_matches += count
                    if total_matches >= 5000:
                        break
            if rows:
                files.append((path, rows))
            if total_matches >= 5000:
                break
        return files, total_matches

    def _show_results(self, root, files, total_matches):
        items = []
        for path, rows in files:
            path_str = str(path)
            file_item = QtWidgets.QTreeWidgetItem([f'{path.name}  ·  {path.relative_to(root)}'])
            file_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, path_str)
            file_item.setToolTip(0, path_str)
            children = []
            for line_no, snippet in rows:
                child = QtWidgets.QTreeWidgetItem([f'{line_no}:  {snippet}'])
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole, path_str)
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, line_no)
                child.setToolTip(0, f'{path}:{line_no}\n{snippet}')
                children.append(child)
            file_item.addChildren(children)
            items.append(file_item)

        self.result_count.setText(f'Совпадений: {total_matches} в {len(files)} файлах')
        if not items:
            items.append(QtWidgets.QTreeWidgetItem(['Ничего не найдено']))
        self.results.addTopLevelItems(items)
        self.results.expandAll()

    def _open_editor_paths(self) -> set:
        """Absolute paths of files currently open in editor tabs (skip them on replace)."""
        open_paths = set()
        window = self.window()
        getter = getattr(window, 'get_open_file_paths', None)
        if callable(getter):
            try:
                for p in getter() or []:
                    open_paths.add(os.path.normcase(str(Path(p).resolve())))
            except Exception:
                pass
        return open_paths

    def replace_all(self):
        if self._replacement is not None:
            return
        query = self.query.text()
        if not query.strip():
            return
        try:
            pattern = self._compile_pattern(query)
        except re.error as exc:
            QtWidgets.QMessageBox.warning(self, 'Неверный шаблон поиска', str(exc))
            return
        replacement = self.replace.text()
        substitute = replacement if self.regex.isChecked() else lambda _match: replacement
        try:
            pattern.sub(substitute, "")
        except (re.error, IndexError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Неверная замена', str(exc))
            return
        from services.background import run_async

        job = _ReplacementJob(self.root_path.resolve(), self._open_editor_paths())
        self._replacement = job
        self._search_cancel.set()
        self._pending_results = None
        self.replace_button.setEnabled(False)
        self.cancel_replace_button.setEnabled(True)
        self.cancel_replace_button.show()
        self.result_count.setText("Замена по проекту…")
        work = self._replace_in_files
        run_async(lambda: work(job, pattern, substitute),
                  on_done=lambda result: self._finish_replacement(job, result),
                  on_error=lambda error: self._finish_replacement(job, (0, 0, [], [error])))

    def stop_replace(self):
        if self._replacement is not None:
            self._replacement.cancel.set()
            self.cancel_replace_button.setEnabled(False)
            self.result_count.setText("Останавливаем замену…")

    def cancel_background(self):
        self._search_cancel.set()
        if self._replacement is not None:
            self._replacement.cancel.set()
            self._replacement = None

    def protect_open_files(self):
        job = self._replacement
        if job is not None:
            paths = self._open_editor_paths()
            with job.lock:
                job.protected.update(paths)

    @classmethod
    def _replace_in_files(cls, job, pattern, substitute):
        from core.file_snapshot import snapshot_bytes, write_if_unchanged

        changes = 0
        written = 0
        skipped_open = []
        failed_files = []
        for path, text, encoding, bom in cls._iter_text_files(job.root, job.cancel):
            if job.cancel.is_set():
                break
            new_text, count = pattern.subn(substitute, text)
            if not count:
                continue
            try:
                resolved = path.resolve()
                if path.is_symlink() or not resolved.is_relative_to(job.root):
                    continue
                original = bom + text.encode(encoding)
                payload = bom + new_text.encode(encoding)
                with job.lock:
                    if os.path.normcase(str(resolved)) in job.protected:
                        skipped_open.append(resolved.relative_to(job.root).as_posix())
                        continue
                if job.cancel.is_set():
                    break
                write_if_unchanged(resolved, payload, snapshot_bytes(original))
                changes += count
                written += 1
            except UnicodeEncodeError:
                failed_files.append(f"{path.name} · замена не представима в {encoding}")
            except OSError as exc:
                failed_files.append(f"{path.name} · {exc}")
        return changes, written, skipped_open, failed_files

    def _finish_replacement(self, job, result):
        if self._replacement is not job:
            return
        self._replacement = None
        self.replace_button.setEnabled(True)
        self.cancel_replace_button.hide()
        changes, written, skipped_open, failed_files = result
        if job.switched_workspace:
            status = getattr(self.window(), "status_bar", None)
            if status is not None:
                status.showMessage(f"Замена в прежнем проекте остановлена · изменено совпадений: {changes}")
            self.refresh_if_needed()
            return
        self.search()
        message = f"Заменено совпадений: {changes}. Изменено файлов: {written}."
        if job.cancel.is_set():
            message = "Замена остановлена. " + message
        if skipped_open:
            preview = ', '.join(sorted(set(skipped_open))[:5])
            message += (f'\n\nПропущено файлов, открытых в редакторе: {len(set(skipped_open))}'
                        f' ({preview}). Сохраните и закройте их, затем повторите замену.')
        if failed_files:
            message += f"\n\nПропущено из-за ошибок: {len(failed_files)}\n" + "\n".join(failed_files[:5])
        QtWidgets.QMessageBox.information(self, 'Заменить всё', message)

    def _open_item(self, item, column):
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not path:
            return
        line = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
        if line is not None:
            self.open_match_requested.emit(path, int(line))
        else:
            self.open_file_requested.emit(path)
