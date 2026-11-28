"""Structured, live viewer for agent team journals."""

import html
import re

from PyQt6 import QtCore, QtWidgets


DIFF_COLORS = {"+": "#3fb950", "-": "#f85149", "@": "#58a6ff"}


class AgentJournalWidget(QtWidgets.QWidget):
    """Journal of one agent run plus undo controls for its recorded edits.

    ``edits`` is a list of plain dicts (``id``, ``path``, ``diff``, ``state``)
    built from the snapshot store by the team tab; the widget itself only
    renders them and reports the requested action through signals.
    """

    undoEditRequested = QtCore.pyqtSignal(int)
    rollbackRunRequested = QtCore.pyqtSignal()

    def __init__(self, title, text="", parent=None, edits=None):
        super().__init__(parent)
        self.setProperty("pyscribeSpecial", "agentJournal")
        self.setObjectName("agentJournalView")
        self._edits = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)
        heading = QtWidgets.QLabel(f"Журнал · {title}")
        heading.setObjectName("agentJournalTitle")
        layout.addWidget(heading)
        self.count_label = QtWidgets.QLabel()
        self.count_label.setObjectName("agentJournalCount")
        layout.addWidget(self.count_label)

        self.edits_panel = QtWidgets.QWidget()
        self.edits_panel.setObjectName("agentJournalEditsPanel")
        edits_layout = QtWidgets.QVBoxLayout(self.edits_panel)
        edits_layout.setContentsMargins(0, 0, 0, 0)
        edits_layout.setSpacing(6)
        self.edits_label = QtWidgets.QLabel()
        self.edits_label.setObjectName("agentJournalEditsTitle")
        edits_layout.addWidget(self.edits_label)
        self.edit_list = QtWidgets.QListWidget()
        self.edit_list.setObjectName("agentJournalEdits")
        self.edit_list.setMaximumHeight(110)
        self.edit_list.currentRowChanged.connect(self._show_edit_diff)
        self.edit_list.itemDoubleClicked.connect(lambda _item: self._request_undo())
        edits_layout.addWidget(self.edit_list)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(6)
        self.undo_button = QtWidgets.QPushButton("Отменить эту правку")
        self.undo_button.setObjectName("agentJournalUndo")
        self.undo_button.setToolTip("Вернуть файл к состоянию до этой правки агента")
        self.undo_button.clicked.connect(self._request_undo)
        actions.addWidget(self.undo_button)
        self.rollback_button = QtWidgets.QPushButton("Откатить всю сессию")
        self.rollback_button.setObjectName("agentJournalRollback")
        self.rollback_button.setToolTip("Вернуть все файлы сессии к состоянию до запуска агента")
        self.rollback_button.clicked.connect(self.rollbackRunRequested.emit)
        actions.addWidget(self.rollback_button)
        actions.addStretch(1)
        edits_layout.addLayout(actions)
        self.edits_hint = QtWidgets.QLabel(
            "Двойной клик по правке показывает дифф; «Отменить» возвращает только её.")
        self.edits_hint.setObjectName("agentJournalEditsHint")
        self.edits_hint.setWordWrap(True)
        edits_layout.addWidget(self.edits_hint)
        layout.addWidget(self.edits_panel)

        self.entries = QtWidgets.QTreeWidget()
        self.entries.setObjectName("agentJournalEntries")
        self.entries.setHeaderLabels(["Время", "Событие"])
        self.entries.setRootIsDecorated(False)
        self.entries.setAlternatingRowColors(True)
        self.entries.setUniformRowHeights(True)
        self.entries.setColumnWidth(0, 100)
        self.entries.header().setStretchLastSection(True)
        self.entries.currentItemChanged.connect(self._show_details)
        layout.addWidget(self.entries, 3)
        self.details = QtWidgets.QTextBrowser()
        self.details.setObjectName("agentJournalDetails")
        layout.addWidget(self.details, 2)
        self.set_entries(text)
        self.set_edits(edits or [])

    def set_entries(self, text):
        self.entries.clear()
        for entry in re.split(r"(?m)(?=^\d\d:\d\d:\d\d  )", text or ""):
            if entry.strip():
                self.append_entry(entry, scroll=False)
        self.entries.scrollToBottom()

    def append_entry(self, entry, scroll=True):
        stamp, _, content = entry.partition("  ")
        if not re.fullmatch(r"\d\d:\d\d:\d\d", stamp):
            content = entry
            stamp = ""
        summary = content.strip().splitlines()[0] if content.strip() else "Событие"
        item = QtWidgets.QTreeWidgetItem([stamp, summary.strip()])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, content)
        item.setToolTip(1, summary.strip())
        self.entries.addTopLevelItem(item)
        self.count_label.setText(f"Записей: {self.entries.topLevelItemCount()}")
        if scroll:
            self.entries.scrollToItem(item)
            self.entries.setCurrentItem(item)

    def set_edits(self, edits):
        """Refresh the «правки агента» section (list of snapshot dicts)."""
        current = self.current_edit_id()
        self._edits = [dict(edit) for edit in (edits or [])]
        self.edit_list.blockSignals(True)
        self.edit_list.clear()
        index = -1
        for position, edit in enumerate(self._edits):
            item = QtWidgets.QListWidgetItem(self._edit_title(edit))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, edit)
            item.setToolTip(str(edit.get("path") or ""))
            self.edit_list.addItem(item)
            if edit.get("id") == current and index < 0:
                index = position
        self.edit_list.blockSignals(False)
        has_edits = bool(self._edits)
        self.edits_panel.setVisible(has_edits)
        self.edits_label.setText(f"Правки агента · {len(self._edits)}")
        if has_edits:
            self.edit_list.setCurrentRow(index if index >= 0 else 0)
        else:
            self.details.clear()

    def edits(self):
        return [dict(edit) for edit in self._edits]

    def current_edit_id(self):
        item = self.edit_list.currentItem()
        data = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        return data.get("id") if isinstance(data, dict) else None

    def _edit_title(self, edit):
        path = str(edit.get("path") or "файл")
        added = edit.get("added")
        removed = edit.get("removed")
        suffix = ""
        if isinstance(added, int) and isinstance(removed, int):
            suffix = f"   +{added} −{removed}"
        state = {"pending": " · не подтверждено", "restored": " · отменено"}.get(edit.get("state"), "")
        return f"{path}{suffix}{state}"

    def _show_edit_diff(self, row):
        if row is None or not (0 <= row < len(self._edits)):
            return
        edit = self._edits[row]
        stamp = {"pending": "применение не подтверждено", "restored": "правка отменена"}.get(
            edit.get("state"), "снимок правки")
        self.details.setHtml(
            f"<b>{html.escape(str(edit.get('path') or 'файл'))}</b> · {html.escape(stamp)}"
            + self._diff_html(edit.get("diff") or "")
        )

    @staticmethod
    def _diff_html(text):
        rows = []
        for line in str(text).splitlines():
            color = DIFF_COLORS.get(line[:1])
            body = html.escape(line) or "&nbsp;"
            rows.append(f"<span style='color:{color}'>{body}</span>" if color else body)
        return "<pre style='white-space:pre-wrap'>" + "<br>".join(rows) + "</pre>"

    def _request_undo(self):
        edit_id = self.current_edit_id()
        if edit_id is not None:
            self.undoEditRequested.emit(int(edit_id))

    def _show_details(self, item, _previous=None):
        if item is None:
            self.details.clear()
            return
        body = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
        self.details.setHtml(
            f"<b>{html.escape(item.text(0))}</b><pre style='white-space:pre-wrap'>{html.escape(body)}</pre>"
        )
