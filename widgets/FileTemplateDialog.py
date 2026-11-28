"""PyScribe widgets/FileTemplateDialog.py: выбор шаблона для нового файла.

Диалог показывается при создании файла (Ctrl+N и контекстное меню проводника):
слева — список шаблонов проекта из ``.codelane/templates/``, справа — предпросмотр
уже с подставленными именем файла, датой и автором.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from core.file_templates import FileTemplate, render_template, template_for_path


class FileTemplateDialog(QtWidgets.QDialog):
    """Выбор шаблона нового файла с живым предпросмотром."""

    def __init__(
        self,
        templates,
        *,
        file_path=None,
        root=None,
        author=None,
        parent=None,
    ):
        super().__init__(parent)
        self.templates = list(templates or [])
        self.file_path = Path(file_path) if file_path else None
        self.root = root
        self.author = author
        self.selected_template: FileTemplate | None = None
        self.setWindowTitle("Шаблон нового файла")
        self.setModal(True)
        self.setObjectName("fileTemplateDialog")
        self.resize(820, 520)
        self._build_ui()
        self._fill_templates()
        self._apply_style()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QtWidgets.QLabel(self._header_text(), self)
        title.setObjectName("fileTemplateHeader")
        title.setWordWrap(True)
        layout.addWidget(title)

        hint = QtWidgets.QLabel(
            "Шаблоны хранятся в .codelane/templates/ — их можно править под стиль проекта.",
            self,
        )
        hint.setObjectName("fileTemplateHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)

        self.list = QtWidgets.QListWidget(self)
        self.list.setObjectName("fileTemplateList")
        self.list.setFixedWidth(260)
        body.addWidget(self.list)

        self.preview = QtWidgets.QPlainTextEdit(self)
        self.preview.setObjectName("fileTemplatePreview")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.preview.setFont(font)
        body.addWidget(self.preview, 1)

        layout.addLayout(body, 1)

        self.buttons = QtWidgets.QDialogButtonBox(self)
        self.create_button = self.buttons.addButton(
            "Создать", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.cancel_button = self.buttons.addButton(
            "Отмена", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole
        )
        layout.addWidget(self.buttons)

        self.list.currentItemChanged.connect(self._update_preview)
        self.list.itemDoubleClicked.connect(lambda _item: self._accept_current())
        self.create_button.clicked.connect(self._accept_current)
        self.cancel_button.clicked.connect(self.reject)

    def _header_text(self) -> str:
        if self.file_path is None:
            return "Выберите заготовку для нового файла"
        return f"Шаблон для файла: {self.file_path.name}"

    def _fill_templates(self) -> None:
        blank = QtWidgets.QListWidgetItem("Без шаблона (пустой файл)")
        blank.setData(QtCore.Qt.ItemDataRole.UserRole, "")
        blank.setToolTip("Создать пустой файл")
        self.list.addItem(blank)

        for template in self.templates:
            item = QtWidgets.QListWidgetItem(template.title)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, template.id)
            if template.description:
                item.setToolTip(template.description)
            item.setStatusTip(template.description)
            self.list.addItem(item)

        self._select_default()

    def _select_default(self) -> None:
        target = template_for_path(self.templates, self.file_path) if self.file_path else None
        wanted = target.id if target is not None else ""
        row = 0
        for index in range(self.list.count()):
            item = self.list.item(index)
            if (item.data(QtCore.Qt.ItemDataRole.UserRole) or "") == wanted:
                row = index
                break
        self.list.setCurrentRow(row)

    def _update_preview(self, *_args) -> None:
        self.preview.setPlainText(self._preview_text(self._current_template()))

    def _preview_text(self, template: FileTemplate | None) -> str:
        if template is None:
            return "Файл будет создан пустым."
        return render_template(
            template,
            self.file_path or "new_file.py",
            root=self.root,
            author=self.author,
        )

    def _current_template(self) -> FileTemplate | None:
        item = self.list.currentItem()
        if item is None:
            return None
        template_id = item.data(QtCore.Qt.ItemDataRole.UserRole) or ""
        if not template_id:
            return None
        for template in self.templates:
            if template.id == template_id:
                return template
        return None

    def _accept_current(self) -> None:
        self.selected_template = self._current_template()
        self.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QDialog#fileTemplateDialog { background: #252526; }
            QLabel#fileTemplateHeader { color: #ffffff; font-size: 14px; font-weight: 600; }
            QLabel#fileTemplateHint { color: #9a9a9a; font-size: 11px; }
            QListWidget#fileTemplateList { background: #1e1e1e; color: #cccccc; border: 1px solid #3c3c3c;
                                           border-radius: 8px; outline: none; padding: 4px; }
            QListWidget#fileTemplateList::item { padding: 8px 10px; border-radius: 6px; margin: 1px 2px; }
            QListWidget#fileTemplateList::item:selected { background: #094771; color: #ffffff; }
            QPlainTextEdit#fileTemplatePreview { background: #1e1e1e; color: #d4d4d4; border: 1px solid #3c3c3c;
                                                 border-radius: 8px; padding: 8px; }
            QPushButton { background: #0e639c; color: #ffffff; border: 1px solid #1177bb;
                          border-radius: 6px; min-width: 96px; min-height: 28px; padding: 4px 14px; }
            QPushButton:hover { background: #1177bb; }
        """)

    # -------------------------------------------------------------- helpers
    def template_body(self) -> str:
        """Готовый текст файла: пустая строка для пункта «Без шаблона»."""
        if self.selected_template is None:
            return ""
        return self._preview_text(self.selected_template)


def ask_template(parent=None, templates=None, *, file_path=None, root=None, author=None):
    """Показать диалог и вернуть текст файла; ``None`` — пользователь отменил."""
    dialog = FileTemplateDialog(
        templates or [],
        file_path=file_path,
        root=root,
        author=author,
        parent=parent,
    )
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return dialog.template_body()
