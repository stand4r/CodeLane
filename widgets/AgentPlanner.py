"""Persistent one-shot assignments for an open project."""
from pathlib import Path
import time

from PyQt6 import QtCore, QtWidgets

from core.job_recovery import cache_dir as recovery_cache_dir
from core.logging_config import get_logger
from design.colors import COLORS
from services.team_cache import TeamCache

logger = get_logger("agent_planner")

#: Подписи состояний задания в дереве планировщика.
STATES = {"queued": "Ожидает", "running": "Выполняется", "done": "Завершено", "paused": "Приостановлено"}

#: Подпись приостановленного задания, у которого записана причина остановки.
PAUSED_INTERRUPTED = "Приостановлено · прервано"


class AgentPlanner(QtWidgets.QWidget):
    @staticmethod
    def cache_dir():
        """Каталог общего хранилища команды — делегат ``core.job_recovery.cache_dir``."""
        return recovery_cache_dir()

    def __init__(self, team):
        super().__init__(team)
        self.setObjectName("agentPlanner")
        self.team = team
        self.cache = TeamCache(self.cache_dir())
        self.active = None
        self.editing = False
        self.signature = None
        self._root, self._jobs = None, []
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.collapse_button = QtWidgets.QToolButton()
        self.collapse_button.clicked.connect(self._toggle_collapsed)
        row.addWidget(self.collapse_button)
        title = QtWidgets.QLabel("Отложенные задачи")
        title.setObjectName("agentPlannerTitle")
        row.addWidget(title)
        row.setSpacing(6)
        row.addStretch()
        help_button = QtWidgets.QToolButton()
        help_button.setText("?")
        help_button.setToolTip("Как работают отложенные задачи")
        help_button.clicked.connect(self.show_help)
        row.addWidget(help_button)
        add = QtWidgets.QToolButton()
        add.setObjectName("agentPlannerAdd")
        add.setText("+")
        add.setToolTip("Запланировать задачу для ИИ-агента")
        add.setFixedSize(22, 22)
        add.setStyleSheet("QToolButton#agentPlannerAdd { padding: 0; font-size: 14px; }")
        add.clicked.connect(lambda: self.edit())
        row.addWidget(add)
        box.addLayout(row)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setObjectName("agentPlannerTree")
        self.tree.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.tree.setIndentation(16)
        self.empty_label = QtWidgets.QLabel("Нет отложенных задач\nНажмите +, чтобы добавить", self.tree.viewport())
        self.empty_label.setObjectName("agentGraphEmpty")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tree.viewport().installEventFilter(self)
        self.tree.setHeaderHidden(True)
        self.tree.setMaximumHeight(160)
        self.tree.itemDoubleClicked.connect(
            lambda item, _: self.edit(item.data(0, QtCore.Qt.ItemDataRole.UserRole)))
        box.addWidget(self.tree)
        self._set_collapsed(True)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        QtCore.QTimer.singleShot(0, self.refresh)

    def _reload(self):
        """Перечитать задания текущего проекта без heartbeat/recover."""
        root = self.team._agent_panel._project_root()
        self._root = root
        self._jobs = list(self.cache.jobs(root) if root else [])

    def _redraw(self):
        """Перестроить дерево заданий текущего проекта."""
        root = self._root
        jobs = self._jobs
        signature = (root, tuple(tuple(j) for j in jobs))
        if signature == self.signature:
            return
        self.tree.clear()
        self.empty_label.setText("Нет отложенных задач\nНажмите +, чтобы добавить")
        self.empty_label.setVisible(not jobs)
        self.empty_label.setGeometry(self.tree.viewport().rect())
        for job in jobs:
            title = job["title"] or "(без названия)"
            due = QtCore.QDateTime.fromSecsSinceEpoch(int(job["due"])).toString("dd.MM HH:mm")
            item = QtWidgets.QTreeWidgetItem(self.tree, [f"{title} · {self._status(job)}"])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, dict(job))
            reason = self._reason(job)
            prompt = job["prompt"] or ""
            condition = f"\nПосле появления файла: {job['required_file']}" if job["required_file"] else ""
            item.setToolTip(0, f"Не раньше: {due}{condition}\n\n{prompt}" +
                            (f"\n\nПричина: {reason}" if reason else ""))
        self.signature = signature

    def _toggle_collapsed(self):
        self._set_collapsed(not self.tree.isHidden())
        if not self.tree.isHidden():
            from widgets.animations import slide_reveal
            slide_reveal(self.tree, 160)

    def _set_collapsed(self, collapsed: bool):
        self.tree.setVisible(not collapsed)
        self.collapse_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if collapsed else QtCore.Qt.ArrowType.UpArrow)
        self.collapse_button.setToolTip("Показать отложенные задачи" if collapsed else "Скрыть отложенные задачи")

    @staticmethod
    def _reason(job):
        """Причина приостановки задания: строка или None, если её нет."""
        try:
            return (job["reason"] or "").strip() or None
        except (IndexError, KeyError):
            return None

    def _status(self, job):
        """Подпись состояния для дерева; ``paused`` с причиной помечается как прерванное."""
        if job["state"] == "paused" and self._reason(job):
            return PAUSED_INTERRUPTED
        return STATES.get(job["state"] or "", job["state"] or "Неизвестно")

    def show_help(self):
        QtWidgets.QMessageBox.information(
            self, "Отложенные задачи",
            "Задача запускается один раз, когда наступит указанное время. Если задан файл-условие, "
            "агент дождётся, пока этот файл появится в открытом проекте.\n\n"
            "CodeLane должен оставаться открытым, проект — открытым, а ИИ-агент — настроенным. "
            "Задача ждёт, пока агент занят. При закрытом приложении работа не выполняется; "
            "после следующего запуска просроченная задача будет проверена снова.\n\n"
            "Например: завтра проверить тесты после ночной сборки или подготовить обзор "
            "после появления отчёта. Результат появится в чате агента. Работа с файлами "
            "зависит от выбранных разрешений агента.")

    def _restore_interrupted(self):
        """Подтвердить жизнь экземпляра и вернуть прерванные задания в «Приостановлено».

        Сбой хранилища не должен ломать отрисовку планировщика.
        """
        try:
            self.cache.heartbeat()
            self.cache.recover()
        except Exception:  # noqa: BLE001 - диагностика в журнале вместо исключения в таймере Qt
            logger.exception("Не удалось восстановить отложенные задания")

    def refresh(self):
        root = self.team._agent_panel._project_root()
        self._restore_interrupted()
        self._reload()
        self._redraw()
        if self.editing or self.active or self.team.busy or self.team._edits or self.team._question or getattr(self.team._agent_panel, "_worker", None):
            return
        for job in self._jobs:
            if job["state"] != "queued" or job["due"] > time.time():
                continue
            if job["required_file"] and not (Path(root) / job["required_file"]).is_file():
                continue
            if not self.cache.claim(root, job["id"]):
                continue  # задание уже забрал другой экземпляр — пробуем следующее
            self.active = (root, job["id"])
            if not self.team.submit_scheduled(f"{job['title']}\n\n{job['prompt']}"):
                self.finished(False)
            break

    def eventFilter(self, watched, event):
        if watched is self.tree.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            self.empty_label.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def finished(self, success):
        if self.active:
            self.cache.state(*self.active, "done" if success else "paused", "running")
            self.active = None

    def edit(self, job=None):
        root = self.team._agent_panel._project_root()
        if not root:
            self.team._agent_panel._append_notice("Сначала откройте проект.", "warn")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Отложенная задача")
        form = QtWidgets.QFormLayout(dialog)
        help_text = QtWidgets.QLabel(
            "ИИ-агент выполнит задачу один раз, когда наступит время и открыт этот проект. "
            "CodeLane должен оставаться запущенным. Результат появится в чате агента.")
        help_text.setWordWrap(True)
        form.addRow(help_text)
        title = QtWidgets.QLineEdit()
        title.setPlaceholderText("Например: Проверить тесты после сборки")
        prompt = QtWidgets.QPlainTextEdit()
        prompt.setPlaceholderText("Что агент должен сделать и какой результат вы ожидаете")
        due = QtWidgets.QDateTimeEdit(QtCore.QDateTime.currentDateTime().addSecs(300))
        due.setCalendarPopup(True)
        due.setDisplayFormat("dd.MM.yyyy HH:mm")
        required = QtWidgets.QLineEdit()
        required.setPlaceholderText("Например: reports/build.txt (необязательно)")
        required.setToolTip("Относительный путь в проекте. Задача запустится после появления файла.")
        for name, widget in (("Название", title), ("Задание агенту", prompt), ("Не раньше", due),
                             ("Дождаться файла", required)):
            form.addRow(name, widget)
        reason = self._reason(job) if job else None
        if reason:
            note = QtWidgets.QLabel(reason)
            note.setObjectName("agentPlannerReason")
            note.setWordWrap(True)
            note.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            note.setStyleSheet(f"color: {COLORS['text_muted']};")
            form.addRow("Причина", note)
        if job:
            title.setText(job["title"])
            prompt.setPlainText(job["prompt"])
            due.setDateTime(QtCore.QDateTime.fromSecsSinceEpoch(job["due"]))
            required.setText(job["required_file"])
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save |
                                             QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.rejected.connect(dialog.reject)
        def save():
            if not title.text().strip() or not prompt.toPlainText().strip():
                return
            try:
                self.cache.save_job(root, title.text().strip(), "agent", prompt.toPlainText().strip(),
                                    due.dateTime().toSecsSinceEpoch(), required.text().strip(), job["id"] if job else None)
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(dialog, "Условие", str(exc))
                return
            dialog.accept()
        buttons.accepted.connect(save)
        owner_alive = bool(job) and self.cache.alive(job["owner"])
        foreign_live = bool(job) and job["state"] == "running" and owner_alive and job["owner"] != self.cache.owner
        save_button = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save)
        if job and job["state"] == "running" and owner_alive:
            # Чужое активное задание нельзя перезапустить: перезапись вернула бы его в очередь.
            save_button.setEnabled(False)
            save_button.setToolTip("Задание выполняется в другом экземпляре CodeLane" if foreign_live
                                   else "Задание выполняется — дождитесь завершения")
        removed = False
        if job:
            busy = job["state"] == "running" and owner_alive
            remove = buttons.addButton("Удалить задание", QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setObjectName("agentPlannerDelete")
            remove.setAutoDefault(False)
            remove.setEnabled(not busy)
            remove.setStyleSheet(f"color: {COLORS['danger']};")
            remove.setToolTip("Задание выполняется в другом экземпляре CodeLane" if foreign_live
                              else ("Задание выполняется — удалить нельзя. Дождитесь завершения задания." if busy
                                    else "Удалить задание из списка отложенных (журналы и снимки сохранятся)"))
            def delete():
                nonlocal removed
                confirm = QtWidgets.QMessageBox(dialog)
                confirm.setWindowTitle("Удалить задание")
                confirm.setIcon(QtWidgets.QMessageBox.Icon.Question)
                confirm.setText(f"Вы уверены, что хотите удалить задачу «{job['title']}»?")
                confirm.setInformativeText("Запись исчезнет из списка отложенных задач. "
                                           "Журналы и снимки правок останутся в архиве.")
                confirm.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes |
                                           QtWidgets.QMessageBox.StandardButton.No)
                confirm.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
                confirm.button(QtWidgets.QMessageBox.StandardButton.Yes).setText("Удалить")
                confirm.button(QtWidgets.QMessageBox.StandardButton.No).setText("Отмена")
                if confirm.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
                try:
                    deleted = self.cache.remove_job(root, job["id"])
                except ValueError as exc:
                    QtWidgets.QMessageBox.warning(dialog, "Удаление", str(exc))
                    return
                if not deleted:
                    QtWidgets.QMessageBox.warning(dialog, "Удаление",
                                                  "Задание этого проекта уже удалено или недоступно.")
                    dialog.reject()
                    return
                removed = True
                dialog.reject()
            remove.clicked.connect(delete)
        if job and job["state"] == "queued":
            pause = buttons.addButton("Снять с запуска", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
            pause.clicked.connect(lambda: (self.cache.state(root, job["id"], "paused", "queued"), dialog.accept()))
        if job and job["state"] == "paused":
            restart = buttons.addButton("Запустить снова", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
            restart.setObjectName("agentPlannerRestart")
            restart.setAutoDefault(False)
            restart.setToolTip("Вернуть задание в очередь: владелец и причина остановки сбрасываются")
            restart.clicked.connect(lambda: (self.cache.state(root, job["id"], "queued", "paused"),
                                             dialog.accept()))
        self.editing = True
        try:
            accepted = dialog.exec()
        finally:
            self.editing = False
        if accepted or removed:
            self.refresh()
