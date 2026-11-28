"""Hunk-level review of unstaged changes with partial staging.

The dialog shows ``git diff`` output for one file, lets the user tick the
hunks worth committing and stages exactly those hunks through
``git apply --cached``.  All git access goes through the ``run_git`` callable
of the source control panel, so credentials and environment stay in one place.
"""

import os
import tempfile

from PyQt6 import QtCore, QtGui, QtWidgets

from core.git_diff import build_patch, parse_diff


class DiffReviewDialog(QtWidgets.QDialog):
    """Review unstaged changes hunk by hunk and stage the selected ones."""

    staged = QtCore.pyqtSignal()

    def __init__(self, parent=None, root=None, run_git=None, path=None):
        super().__init__(parent)
        self.setWindowTitle("Ревью изменений перед коммитом")
        self.setObjectName("diffReviewDialog")
        self.resize(1020, 680)
        self._root = root
        self._git = run_git
        self._diff_file = None
        self._untracked = False
        self._build()
        self.reload_files(select=path)

    # ------------------------------------------------------------------ UI
    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        heading = QtWidgets.QLabel("Ревью изменений")
        heading.setObjectName("diffReviewTitle")
        outer.addWidget(heading)
        self.hint = QtWidgets.QLabel(
            "Отметьте хунки, которые войдут в коммит, и проиндексируйте их. "
            "Остальные изменения останутся в рабочем дереве.")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("diffReviewHint")
        outer.addWidget(self.hint)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        self.files_label = QtWidgets.QLabel("Файлы")
        self.files_label.setObjectName("diffReviewFilesTitle")
        left_layout.addWidget(self.files_label)
        self.files = QtWidgets.QListWidget()
        self.files.setObjectName("diffReviewFiles")
        self.files.currentRowChanged.connect(self._file_selected)
        left_layout.addWidget(self.files, 1)
        splitter.addWidget(left)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        self.file_label = QtWidgets.QLabel("Выберите файл")
        self.file_label.setObjectName("diffReviewFileTitle")
        right_layout.addWidget(self.file_label)
        self.hunks_label = QtWidgets.QLabel("Хунки")
        self.hunks_label.setObjectName("diffReviewHunksTitle")
        right_layout.addWidget(self.hunks_label)
        self.hunks = QtWidgets.QListWidget()
        self.hunks.setObjectName("diffReviewHunks")
        self.hunks.setMaximumHeight(150)
        self.hunks.itemChanged.connect(self._hunk_toggled)
        self.hunks.currentRowChanged.connect(self._hunk_selected)
        right_layout.addWidget(self.hunks)
        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(6)
        self.check_all = QtWidgets.QPushButton("Отметить все")
        self.check_all.clicked.connect(lambda: self._check_hunks(True))
        self.check_none = QtWidgets.QPushButton("Снять отметки")
        self.check_none.clicked.connect(lambda: self._check_hunks(False))
        tools.addWidget(self.check_all)
        tools.addWidget(self.check_none)
        tools.addStretch(1)
        right_layout.addLayout(tools)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setObjectName("diffReviewPreview")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.preview.font()
        font.setFamily("Consolas")
        font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        self.preview.setFont(font)
        right_layout.addWidget(self.preview, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        outer.addWidget(splitter, 1)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("diffReviewStatus")
        self.status.setWordWrap(True)
        buttons.addWidget(self.status, 1)
        self.file_button = QtWidgets.QPushButton("Проиндексировать файл целиком")
        self.file_button.clicked.connect(self.stage_file)
        buttons.addWidget(self.file_button)
        self.stage_button = QtWidgets.QPushButton("Проиндексировать выбранные хунки")
        self.stage_button.setDefault(True)
        self.stage_button.clicked.connect(self.stage_selected)
        buttons.addWidget(self.stage_button)
        close = QtWidgets.QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addLayout(buttons)
        self._update_actions()

    # --------------------------------------------------------------- loading
    def _run(self, args, input_text=None):
        if not callable(self._git):
            return False, "Git недоступен."
        return self._git(args, input_text=input_text)

    def reload_files(self, select=None):
        """Refresh the list of unstaged and untracked files."""
        ok, output = self._run(["diff", "--name-only"])
        tracked = output.splitlines() if ok else []
        ok, output = self._run(["ls-files", "--others", "--exclude-standard"])
        untracked = output.splitlines() if ok else []
        self.files.blockSignals(True)
        self.files.clear()
        target = -1
        for name in tracked + untracked:
            item = QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name in untracked)
            self.files.addItem(item)
            if select and name == select and target < 0:
                target = self.files.count() - 1
        self.files.blockSignals(False)
        total = self.files.count()
        staged_note = "" if total else "Незакоммиченных изменений нет."
        self.files_label.setText(f"Файлы · {total}" if total else "Файлы")
        self.status.setText(staged_note)
        if total:
            self.files.setCurrentRow(target if target >= 0 else 0)
        else:
            self._diff_file = None
            self.hunks.clear()
            self.preview.clear()
            self.file_label.setText("Выберите файл")
            self._update_actions()

    def _file_selected(self, row):
        if row < 0 or row >= self.files.count():
            return
        item = self.files.item(row)
        path = item.text()
        self._untracked = bool(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self._load_file(path)

    def _load_file(self, path):
        self.hunks.clear()
        self._diff_file = None
        if self._untracked:
            self.file_label.setText(f"{path} · новый файл")
            self.hunks_label.setText("Хунки")
            self.preview.setPlainText(
                f"{path}\n\nФайл не отслеживается Git: частичная индексация "
                "недоступна, добавьте его целиком.")
            self.status.setText("Новый файл — доступна только индексация целиком.")
            self._update_actions()
            return
        ok, output = self._run(["diff", "--", path])
        if not ok:
            self.preview.setPlainText(output or "Не удалось получить дифф.")
            self.status.setText(output or "Не удалось получить дифф.")
            self._update_actions()
            return
        files = parse_diff(output)
        self._diff_file = next((item for item in files if item.path == path), None)
        if self._diff_file is None:
            self.file_label.setText(path)
            self.hunks_label.setText("Хунки")
            self.preview.setPlainText("Изменений в файле нет.")
            self._update_actions()
            return
        self.file_label.setText(f"{path}   {self._diff_file.summary()}")
        self.hunks_label.setText(f"Хунки · {len(self._diff_file.hunks)}")
        self.hunks.blockSignals(True)
        for index, hunk in enumerate(self._diff_file.hunks):
            item = QtWidgets.QListWidgetItem(hunk.summary())
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, index)
            self.hunks.addItem(item)
        self.hunks.blockSignals(False)
        self.hunks.setCurrentRow(0)
        self._update_actions()
        self._render_preview()

    def _hunk_selected(self, row):
        self._render_preview()

    def _hunk_toggled(self, _item):
        self._update_actions()
        self._render_preview()

    def _render_preview(self):
        if self._diff_file is None:
            return
        rows = []
        for index, hunk in enumerate(self._diff_file.hunks):
            checked = self._is_checked(index)
            mark = "[x]" if checked else "[ ]"
            rows.append(f"{mark} {hunk.header}")
            for line in hunk.lines:
                rows.append(line if line else " ")
            rows.append("")
        rows.append("── заголовок файла ──")
        rows.extend(self._diff_file.header)
        self.preview.setPlainText("\n".join(rows))

    # --------------------------------------------------------------- staging
    def _is_checked(self, index):
        for row in range(self.hunks.count()):
            item = self.hunks.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == index:
                return item.checkState() == QtCore.Qt.CheckState.Checked
        return False

    def selected_hunks(self):
        if self._diff_file is None:
            return []
        return [index for index in range(len(self._diff_file.hunks)) if self._is_checked(index)]

    def _check_hunks(self, checked):
        state = (QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self.hunks.blockSignals(True)
        for row in range(self.hunks.count()):
            self.hunks.item(row).setCheckState(state)
        self.hunks.blockSignals(False)
        self._update_actions()
        self._render_preview()

    def _update_actions(self):
        selected = self.selected_hunks()
        partial = self._diff_file is not None and bool(self._diff_file.hunks)
        has_file = self._diff_file is not None or self._untracked
        self.stage_button.setEnabled(bool(selected))
        self.file_button.setEnabled(has_file)
        self.check_all.setEnabled(partial)
        self.check_none.setEnabled(partial)
        if partial:
            added = sum(self._diff_file.hunks[index].added for index in selected)
            removed = sum(self._diff_file.hunks[index].removed for index in selected)
            self.stage_button.setText(
                f"Проиндексировать выбранные хунки ({len(selected)}) · +{added} −{removed}")

    def stage_selected(self):
        """Stage the ticked hunks of the current file via ``git apply --cached``."""
        if self._diff_file is None:
            return False
        selected = self.selected_hunks()
        patch = build_patch(self._diff_file, selected)
        if not patch:
            self.status.setText("Отметьте хотя бы один хунк.")
            return False
        ok, output = self._apply_patch(patch)
        if ok:
            self.status.setText(f"Проиндексировано хунков: {len(selected)}.")
            self.staged.emit()
            self._refresh_current()
        else:
            self.status.setText(output.strip() or "git apply завершился с ошибкой.")
            QtWidgets.QMessageBox.warning(
                self, "Частичная индексация",
                "Не удалось проиндексировать выбранные хунки.\n\n" + (output.strip() or "Неизвестная ошибка."))
        return ok

    def _apply_patch(self, patch):
        """Run ``git apply --cached`` on a temporary patch file (stable EOL)."""
        handle, name = tempfile.mkstemp(prefix="codelane-hunks-", suffix=".patch")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(patch.encode("utf-8"))
            return self._run(["apply", "--cached", "--whitespace=nowarn", name])
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    def stage_file(self):
        """Stage the whole current file (also works for untracked files)."""
        row = self.files.currentRow()
        if row < 0:
            return False
        path = self.files.item(row).text()
        ok, output = self._run(["add", "--", path])
        if ok:
            self.status.setText(f"Файл проиндексирован: {path}")
            self.staged.emit()
            self.reload_files(select=path)
        else:
            self.status.setText(output.strip() or "Не удалось проиндексировать файл.")
        return ok

    def _refresh_current(self):
        row = self.files.currentRow()
        path = self.files.item(row).text() if row >= 0 else None
        self.reload_files(select=path)
