"""Control panel for a single project-scoped agent team run."""

from functools import partial
from threading import Event
from uuid import uuid4

from PyQt6 import QtCore, QtWidgets

from services.agent_team import run_team
from services.background import run_async
from widgets.AgentGraph import AgentGraph
from widgets.AgentPlanner import AgentPlanner

ROLE_DETAILS = {
    "Руководитель": "Распределяет задачи и проверяет общий результат.",
    "Постановка задачи": "Формулирует критерии, поручения и вопросы.",
    "Дизайнер": "Проверяет поведение и оформление интерфейса.",
    "Тестировщик": "Ищет сценарии для проверки изменений.",
    "Код": "Изучает реализацию и зависимости.",
    "Ревью": "Проверяет выводы и ищет проблемы.",
    "Документация": "Изучает документацию проекта.",
}


class AgentTeamTab(QtWidgets.QWidget):
    agent_updated = QtCore.pyqtSignal(int, str, str, str)
    report_updated = QtCore.pyqtSignal(int, str)
    action_logged = QtCore.pyqtSignal(int, str)
    relationship_updated = QtCore.pyqtSignal(int, int, object)

    def __init__(self, agent_panel, parent=None):
        super().__init__(parent)
        self.setProperty("pyscribeSpecial", "agentTeam")
        self._agent_panel = agent_panel
        self._cancel = None
        self._goal = ""
        self._root = None
        self._question = ""
        self._cards = {}
        self._model = ""
        self._logs = {}
        self._log_views = {}
        self._edits = []
        self._applying = None
        self._pending = []
        self._last_result = ""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(agent_panel)
        agent_panel.show()
        self.idea = agent_panel.input
        details = QtWidgets.QWidget()
        details.setObjectName("agentWorkspacePanel")
        right = QtWidgets.QVBoxLayout(details)
        right.setContentsMargins(12, 8, 12, 8)
        right.setSpacing(8)

        controls = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Команда")
        title.setObjectName("agentWorkspaceTitle")
        controls.addWidget(title)
        controls.addStretch()
        self.team_mode = QtWidgets.QPushButton()
        self.team_mode.setCheckable(True)
        self.team_mode.setObjectName("agentModeButton")
        self.team_mode.toggled.connect(self._update_mode)
        self.team_mode.setChecked(True)
        self.stop_button = QtWidgets.QPushButton("Стоп")
        self.stop_button.setObjectName("agentStopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.team_mode)
        controls.addWidget(self.stop_button)
        right.addLayout(controls)
        self.activity = QtWidgets.QLabel("Готов к задаче")
        self.activity.setObjectName("agentWorkspaceStats")
        self.activity.setWordWrap(True)
        right.addWidget(self.activity)
        self.pending_list = QtWidgets.QListWidget()
        self.pending_list.setObjectName("agentTaskQueue")
        self.pending_list.setMaximumHeight(90)
        self.pending_list.setToolTip("Двойной клик удаляет задачу из очереди")
        self.pending_list.itemDoubleClicked.connect(self._remove_pending)
        self.pending_list.hide()
        right.addWidget(self.pending_list)
        self.graph = AgentGraph()
        right.addWidget(self.graph, 1)
        self.stats = QtWidgets.QLabel()
        self.stats.setObjectName("agentWorkspaceStats")
        right.addWidget(self.stats)
        self.changes_button = QtWidgets.QPushButton()
        self.changes_button.setObjectName("agentChangesButton")
        self.changes_button.clicked.connect(self._show_changes)
        self.changes_button.hide()
        right.addWidget(self.changes_button)
        self.applied_button = QtWidgets.QPushButton()
        self.applied_button.setObjectName("agentChangesButton")
        self.applied_button.clicked.connect(self.show_snapshots)
        self.applied_button.hide()
        right.addWidget(self.applied_button)
        self.planner = AgentPlanner(self)
        right.addWidget(self.planner)
        split.addWidget(details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([660, 380])
        layout.addWidget(split)
        self.agent_updated.connect(self._update_card, QtCore.Qt.ConnectionType.QueuedConnection)
        self.report_updated.connect(self._update_report, QtCore.Qt.ConnectionType.QueuedConnection)
        self.action_logged.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
        self.relationship_updated.connect(self._relationship, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot(int, int, object)
    def _relationship(self, number, parent, dependencies):
        self.graph.set_relationship(number, parent, dependencies)
        self._log(number, f"Родитель: {parent}. Зависимости: {dependencies}")

    def _update_mode(self, team):
        self.team_mode.setText("Режим: команда" if team else "Режим: чат")
        self.team_mode.setToolTip("Нажмите, чтобы переключить режим отправки")
        self.idea.setPlaceholderText("Задача для команды" if team else "Сообщение")

    @property
    def busy(self):
        return self._cancel is not None or self._applying is not None

    @QtCore.pyqtSlot(int, str, str, str)
    def _update_card(self, number, role, goal, status):
        if number not in self._cards:
            title = role if number == 0 else f"{role} · {number}"
            self._cards[number] = self.graph.add_agent(number, title, self._show_report)
        item = self._cards[number]
        if item.status != status:
            self._log(number, f"{role} · {status}\n{goal}")
        item.status = status
        item.update()
        tooltip = f"{ROLE_DETAILS.get(role, role)}\n\n{status}\n{goal}"
        if self._model:
            tooltip += f"\n\nМодель: {self._model}"
        item.setToolTip(tooltip)
        active = sum(node.status in {"Работает", "Координирует", "Запуск"} for node in self._cards.values())
        waiting = sum(node.status.startswith("Ждёт") for node in self._cards.values())
        errors = sum(node.status in {"Ошибка", "Заблокирован"} for node in self._cards.values())
        self.activity.setText(f"{status}\nРаботают {active} · ожидают {waiting} · ошибки {errors}")
        self.activity.setToolTip(f"{role}\n{goal}")

    def enqueue(self, text):
        root = self._agent_panel._project_root()
        if root != self._root or not root:
            self._agent_panel._append_notice("Проект сменился. Дождитесь завершения текущего запуска.", "warn")
            return False
        self._pending.append((root, text))
        item = QtWidgets.QListWidgetItem(text.splitlines()[0])
        item.setToolTip(text)
        self.pending_list.addItem(item)
        self.pending_list.show()
        return True

    def _remove_pending(self, item):
        index = self.pending_list.row(item)
        self._pending.pop(index)
        self.pending_list.takeItem(index)
        self.pending_list.setVisible(bool(self._pending))

    def _next_pending(self):
        if self.busy or self._question or self._edits or not self._pending:
            return
        root, text = self._pending[0]
        if self._agent_panel._project_root() != root:
            self._pending.clear()
            self.pending_list.clear()
            self.pending_list.hide()
            return
        if self.submit(text):
            self._pending.pop(0)
            self.pending_list.takeItem(0)
            self.pending_list.setVisible(bool(self._pending))

    @QtCore.pyqtSlot(int, str)
    def _update_report(self, number, text):
        item = self._cards.get(number)
        if item is not None:
            item.report = text
            self._log(number, text)
            if item.status == "Ошибка":
                item.setToolTip(item.toolTip() + "\n\nОшибка: " + text)

    @QtCore.pyqtSlot(int, str)
    def _log(self, number, text):
        entry = QtCore.QTime.currentTime().toString("HH:mm:ss") + "  " + str(text)
        self._logs.setdefault(number, []).append(entry)
        view = self._log_views.get(number)
        if view is not None:
            try:
                view.appendPlainText(entry)
            except RuntimeError:
                self._log_views.pop(number, None)

    def _show_report(self, item):
        from PyQt6 import sip
        number = next(n for n, node in self._cards.items() if node is item)
        window = self._agent_panel.main_window
        view = self._log_views.get(number)
        if view is not None and not sip.isdeleted(view):
            group, index = window.find_tab_for_widget(view)
            if group is not None:
                window._set_active_group(group)
                group.setCurrentIndex(index)
                return
        self._log_views[number] = self._journal_tab(item.title, "\n\n".join(self._logs.get(number, [])))

    def _journal_tab(self, title, text):
        window = self._agent_panel.main_window
        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setProperty("pyscribeSpecial", "agentJournal")
        view.setPlainText(text)
        group = window.active_tab_widget
        group.setCurrentIndex(group.addTab(view, "Журнал · " + title))
        return view

    def _archive(self):
        if self._logs and self._root:
            self.planner.cache.archive(self._root, self._run_id, [
                (n, self._cards[n].title, "\n\n".join(entries))
                for n, entries in self._logs.items() if n in self._cards])

    def show_journals(self):
        project = self._agent_panel._project_root()
        rows = self.planner.cache.journals(project)
        if not rows:
            self._agent_panel._append_notice("Сохранённых журналов пока нет.")
            return
        labels = [f"{QtCore.QDateTime.fromSecsSinceEpoch(int(r['created'])).toString('dd.MM HH:mm:ss')}   "
                  f"{r['title']} · {r['run'][:6]}" for r in rows]
        choice, ok = QtWidgets.QInputDialog.getItem(self, "Журналы", "Агент", labels, 0, False)
        if ok:
            row = rows[labels.index(choice)]
            self._journal_tab(row["title"], self.planner.cache.journal(project, row["run"], row["agent"]))

    def submit(self, text, *, scheduled_role=None):
        if self.busy:
            return False
        if not text:
            return
        root = self._agent_panel._project_root()
        if not root:
            self._agent_panel._append_notice("Сначала откройте проект.", "warn")
            return False
        if root != self._root:
            self._question = ""
            self._last_result = ""
        service = self._agent_panel._service()
        problem = service.configuration_problem()
        if problem:
            self._agent_panel._append_notice(problem, "warn")
            return False
        cancel = Event()
        toolbox = self._agent_panel._build_local_tools(cancel.is_set, full_access=True)
        if "read_file" not in {tool["function"]["name"] for tool in toolbox.definitions()}:
            self._agent_panel._append_notice("Включите чтение файлов проекта в разрешениях агента.", "warn")
            return False
        self._root = root
        self._run_id = uuid4().hex
        toolbox.team_cache = self.planner.cache
        toolbox.team_run_id = self._run_id
        self._model = getattr(service, "model", "")
        self._goal = (self._goal + "\n\nУточнение: " + self._question + "\nОтвет: " + text
                      if self._question else text)
        self._question = ""
        self._cancel = cancel
        self.stop_button.setEnabled(True)
        self.stats.clear()
        self.applied_button.hide()
        self._set_edits([])
        self.graph.clear()
        self._cards.clear()
        self._logs.clear()
        self._log_views.clear()
        self._update_card(0, "Руководитель", self._goal, "Запуск")
        self._agent_panel._record_team_message("user", text)
        context = ("\n\nПредыдущая задача и её итог (проверь актуальность по проекту):\n" + self._last_result
                   if self._last_result else "")
        run_async(partial(run_team, service, toolbox, self._goal + context, self.agent_updated.emit, cancel.is_set,
                          report=self.report_updated.emit, trace=self.action_logged.emit,
                          scheduled_role=scheduled_role, relationship=self.relationship_updated.emit),
                  on_done=self._finish, on_error=self._finish)
        return True

    def stop(self):
        self._pending.clear()
        self.pending_list.clear()
        self.pending_list.hide()
        self.planner.finished(False)
        self._archive()
        if self._cancel is not None:
            self._cancel.set()
            self.stop_button.setEnabled(False)
        if self._applying is not None:
            self._applying.set()

    def _set_edits(self, edits):
        self._edits = edits
        self.changes_button.setText(f"Изменения · {len(edits)}")
        self.changes_button.setVisible(bool(edits))

    def show_snapshots(self):
        project = self._agent_panel._project_root()
        if not project or self.busy:
            return
        rows = self.planner.cache.snapshots(project)
        if not rows:
            self._agent_panel._append_notice("За последние 14 дней снимков правок нет.")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Снимки правок · 14 дней")
        dialog.resize(760, 540)
        layout = QtWidgets.QVBoxLayout(dialog)
        files = QtWidgets.QListWidget()
        layout.addWidget(files)
        for row in rows:
            date = QtCore.QDateTime.fromSecsSinceEpoch(int(row["created"])).toString("dd.MM HH:mm")
            suffix = {"restored": " · восстановлено", "pending": " · применение не подтверждено"}
            files.addItem(f"{date}   {row['path']}" + suffix.get(row["state"], ""))
        versions = QtWidgets.QTabWidget()
        original = QtWidgets.QPlainTextEdit()
        diff = QtWidgets.QPlainTextEdit()
        for view, title in ((diff, "Изменения"), (original, "До правки")):
            view.setReadOnly(True)
            versions.addTab(view, title)
        layout.addWidget(versions, 2)
        restore = QtWidgets.QPushButton("Откатить выбранный файл")
        all_files = QtWidgets.QPushButton("Откатить изменения запуска")
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(restore)
        actions.addWidget(all_files)
        layout.addLayout(actions)
        def preview(index):
            try:
                entry, state = self.planner.cache.contents(project, rows[index]["id"])
                original.setPlainText(entry.before)
                diff.setPlainText(entry.diff())
                restore.setEnabled(state != "restored")
                all_files.setEnabled(bool(rows[index]["run"]) and any(
                    row["run"] == rows[index]["run"] and row["state"] != "restored" for row in rows))
            except ValueError as exc:
                original.clear()
                diff.setPlainText(str(exc))
                restore.setEnabled(False)
                all_files.setEnabled(False)
        files.currentRowChanged.connect(preview)
        files.setCurrentRow(0)
        restore.clicked.connect(dialog.accept)
        all_files.clicked.connect(lambda: dialog.done(2))
        accepted = dialog.exec()
        dialog.deleteLater()
        if not accepted or self.busy or project != self._agent_panel._project_root():
            return
        row = rows[files.currentRow()]
        cancel = Event()
        toolbox = self._agent_panel._build_local_tools(cancel.is_set, full_access=True)
        self._applying = cancel
        def finish(message):
            self._applying = None
            if not cancel.is_set():
                self._agent_panel._append_notice(message)
        work = (partial(self.planner.cache.restore_run, project, row["run"], toolbox) if accepted == 2 else
                partial(self.planner.cache.restore, project, row["id"], toolbox))
        run_async(work,
                  on_done=finish, on_error=finish)

    def _show_changes(self):
        if not self._edits or self._applying is not None:
            return
        if self._root != self._agent_panel._project_root():
            self._set_edits([])
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Изменения команды")
        dialog.resize(800, 510)
        layout = QtWidgets.QVBoxLayout(dialog)
        files = QtWidgets.QComboBox()
        edits = list(self._edits)
        for edit in edits:
            files.addItem(edit.path)
        layout.addWidget(files)
        diff = QtWidgets.QPlainTextEdit(edits[0].diff())
        diff.setReadOnly(True)
        diff.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(diff, 1)
        files.currentIndexChanged.connect(lambda index: diff.setPlainText(edits[index].diff()))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        apply = buttons.addButton("Применить к файлу", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(dialog.reject)
        apply.clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        edit = edits[files.currentIndex()]
        dialog.deleteLater()
        if self._root != self._agent_panel._project_root():
            self._set_edits([])
            return
        cancel = Event()
        toolbox = self._agent_panel._build_local_tools(cancel.is_set, full_access=True)
        self._applying = cancel
        self.changes_button.setEnabled(False)

        def finish(message, success):
            self._applying = None
            self.changes_button.setEnabled(True)
            if cancel.is_set():
                return
            if success:
                for number in (0, *edit.agents):
                    self._log(number, "Применено в редакторе:\n" + edit.diff())
                self._archive()
                self._set_edits([pending for pending in self._edits if pending is not edit])
                QtCore.QTimer.singleShot(0, self._next_pending)
            self._agent_panel._append_notice(message, "info" if success else "warn")

        run_async(partial(self.planner.cache.apply, self._root, edit, toolbox, self._run_id),
                  on_done=partial(finish, success=True), on_error=partial(finish, success=False))

    def _record_usage(self, usage) -> bool:
        """Push the run's token usage into the chat dialog counter.

        The team tab keeps its own label (self.stats) for the current run, while
        the chat panel owns the per-dialog counter that save_chat_session
        persists. Keys (prompt_tokens/input_tokens/total_tokens) are interpreted
        only by AIAgentPanel._record_session_stats, so no logic is duplicated
        here. Empty usage is skipped to avoid a bogus "unreported" entry.
        """
        if not usage:
            return False
        record = getattr(self._agent_panel, "_record_session_stats", None)
        if not callable(record):
            return False
        record(usage)
        return True

    def _persist_chat_session(self) -> None:
        save = getattr(self._agent_panel, "save_chat_session", None)
        if callable(save):
            save()

    def _finish(self, result):
        cancelled = self._cancel.is_set()
        self._cancel = None
        self.stop_button.setEnabled(False)
        # Account for the run's tokens before the assistant message is stored:
        # _record_team_message saves the chat session together with the counter.
        usage = {} if isinstance(result, str) else dict(getattr(result, "usage", None) or {})
        usage_recorded = self._record_usage(usage)
        if cancelled or isinstance(result, str) or not result.question:
            self.planner.finished(not cancelled and not isinstance(result, str) and not result.failed_agents
                                  and result.complete)
        if cancelled:
            self.activity.setText("Остановлено")
            if usage_recorded:
                self._persist_chat_session()
            return
        if isinstance(result, str):
            if 0 in self._cards:
                self._update_card(0, "Руководитель", self._goal, "Ошибка")
                self._update_report(0, result)
                self._archive()
            self._agent_panel._append_notice(result, "warn")
            self.activity.setText("Ошибка · очередь ожидает решения")
            if usage_recorded:
                self._persist_chat_session()
            return
        self._question = result.question
        self._set_edits(result.edits)
        self.applied_button.setText(f"Применено файлов · {len(result.changed_files)}")
        self.applied_button.setToolTip("\n".join(result.changed_files))
        self.applied_button.setVisible(bool(result.changed_files))
        answer = result.question or result.answer
        self._log(0, answer)
        for edit in result.edits:
            self._log(0, "Кандидат:\n" + edit.diff())
        self._archive()
        if result.failed_agents:
            answer += f"\n\nЗаданий завершилось с ошибкой: {result.failed_agents}."
        self._agent_panel._record_team_message("assistant", answer)
        self._last_result = self._goal + "\n" + answer
        self.activity.setText("Нужен ваш ответ" if result.question else
                              "Есть незавершённая работа" if not result.complete else
                              "Изменения готовы к просмотру" if result.edits else "Задача завершена")
        if result.question:
            self.idea.setFocus()
        total = result.usage.get("total_tokens")
        self.stats.setText(f"Токены: {total}" if total is not None else "Расход токенов недоступен")
        if result.complete and not result.failed_agents:
            QtCore.QTimer.singleShot(0, self._next_pending)
