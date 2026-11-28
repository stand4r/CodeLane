"""Preview and copy the local diagnostic files."""
import html
import re
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets


class LogShareDialog(QtWidgets.QDialog):
    def __init__(self, directory: Path, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.setWindowTitle("Журналы CodeLane")
        self.resize(980, 620)
        layout = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            "Выберите журнал и проверьте его содержимое перед отправкой. "
            "Скопированный файл можно вставить в папку или приложить к сообщению."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.files = QtWidgets.QListWidget()
        self.files.setAccessibleName("Файлы журналов")
        self.files.setMaximumHeight(138)
        self.files.currentItemChanged.connect(self._preview)
        layout.addWidget(self.files)
        self.level_filter = QtWidgets.QComboBox()
        self.level_filter.addItems(["Все события", "Ошибки", "Предупреждения", "ИИ-агент"])
        self.level_filter.currentIndexChanged.connect(self._apply_filter)
        layout.addWidget(self.level_filter)
        self.preview = QtWidgets.QTreeWidget()
        self.preview.setObjectName("logViewer")
        self.preview.setHeaderLabels(["Время", "Уровень", "Источник", "Событие"])
        self.preview.setRootIsDecorated(False)
        self.preview.setAlternatingRowColors(True)
        self.preview.setUniformRowHeights(True)
        self.preview.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.preview.setAccessibleName("События журнала")
        self.preview.header().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((170, 115, 170)):
            self.preview.setColumnWidth(column, width)
        layout.addWidget(self.preview, 1)
        self.details = QtWidgets.QTextBrowser()
        self.details.setObjectName("logDetails")
        self.details.setMaximumHeight(130)
        self.details.setPlaceholderText("Выберите событие, чтобы прочитать подробности")
        self.preview.currentItemChanged.connect(self._show_details)
        layout.addWidget(self.details)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.copy_button = buttons.addButton("Скопировать файл", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.clicked.connect(self.copy_file)
        refresh = buttons.addButton("Обновить список", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        refresh.clicked.connect(self.refresh)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        current = self.files.currentItem()
        selected = current.text() if current else ""
        self.files.clear()
        try:
            paths = sorted(self.directory.iterdir())
        except OSError:
            paths = []
        for path in paths:
            if re.fullmatch(r".+\.log(?:\.\d+)?", path.name) and path.is_file() and not path.is_symlink():
                self.files.addItem(path.name)
        matches = self.files.findItems(selected, QtCore.Qt.MatchFlag.MatchExactly)
        if matches:
            self.files.setCurrentItem(matches[0])
        elif self.files.count():
            self.files.setCurrentRow(0)
        else:
            self._preview(None)

    def _preview(self, item, previous=None):
        self.copy_button.setEnabled(False)
        self.status.clear()
        self.preview.clear()
        self.details.clear()
        if item is None:
            self.status.setText("Журналы пока отсутствуют.")
            return
        path = self.directory / item.text()
        limit = 256 * 1024
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - limit))
                contents = stream.read(limit).decode("utf-8", errors="replace")
        except OSError as exc:
            self.status.setText(f"Не удалось прочитать журнал. {exc.strerror or exc}")
            return
        lines = contents.splitlines()
        if size > limit and lines:
            lines = lines[1:]
        entries = []
        pattern = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:,\d+)?)\s*\|\s*(?:(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|\s*)?(?:(\S+)\s*\|\s*)?(.*)$")
        for line in lines[-3000:]:
            match = pattern.match(line)
            if match:
                level = match.group(2) or "INFO"
                source = match.group(3) or ""
                message = match.group(4)
                if not match.group(2):
                    message = " ".join(part for part in (source, message) if part)
                    source = "Действия" if path.name.startswith("user-actions") else "Журнал"
                entries.append([match.group(1), level, source, message])
            elif entries:
                entries[-1][3] += "\n" + line
            else:
                entries.append(["", "", "", line])
        self.preview.setUpdatesEnabled(False)
        try:
            self.preview.clear()
            for stamp, level, source, message in entries:
                item = QtWidgets.QTreeWidgetItem([stamp, level, source, message.splitlines()[0] if message else ""])
                item.setToolTip(3, message)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, (level, source))
                if level in {"ERROR", "CRITICAL"}:
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor("#ec7777")))
                elif level == "WARNING":
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor("#e5b567")))
                self.preview.addTopLevelItem(item)
        finally:
            self.preview.setUpdatesEnabled(True)
        self._apply_filter()
        self.preview.scrollToBottom()
        self.copy_button.setEnabled(True)
        if size > limit:
            self.status.setText("Показаны последние 256 КБ. Копируется весь файл.")

    def _apply_filter(self):
        mode = self.level_filter.currentIndex()
        for index in range(self.preview.topLevelItemCount()):
            item = self.preview.topLevelItem(index)
            level, source = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ("", "")
            show = (mode == 0 or mode == 1 and level in {"ERROR", "CRITICAL"}
                    or mode == 2 and level == "WARNING"
                    or mode == 3 and "ai_agent" in source.lower())
            item.setHidden(not show)

    def _show_details(self, item, _previous=None):
        if item is None:
            self.details.clear()
            return
        heading = " · ".join(part for part in (item.text(0), item.text(1), item.text(2)) if part)
        message = item.toolTip(3)
        self.details.setHtml(f"<b>{html.escape(heading)}</b><pre style='white-space:pre-wrap'>{html.escape(message)}</pre>")

    def copy_file(self):
        item = self.files.currentItem()
        if item is None:
            return
        path = self.directory / item.text()
        if not path.is_file() or path.is_symlink():
            self.refresh()
            self.status.setText("Журнал больше недоступен. Выберите файл из обновлённого списка.")
            return
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(str(path.resolve()))])
        QtWidgets.QApplication.clipboard().setMimeData(mime)
        self.status.setText(f"Файл {path.name} скопирован.")
