"""Project agent workspace and live progress."""

from functools import partial
import json
import re
from threading import Event, Lock
from uuid import uuid4

from PyQt6 import QtCore, QtWidgets

from services.ai_budget import clip_utf8, input_tokens, total_tokens
from services.agent_team import run_team
from services.background import run_async
from widgets.AgentPlanner import AgentPlanner

REASONING_BREAK = re.compile(r"(?<=[.!?。！？])\s+|[\r\n]+")
TOOL_PROGRESS = {
    "read_file": "Читаю файл",
    "read_document": "Читаю документ",
    "search_in_project": "Ищу по проекту",
    "list_directory": "Смотрю содержимое папки",
    "edit_file": "Вношу правку",
    "write_file": "Записываю файл",
    "run_project_tests": "Запускаю тесты",
    "pytest": "Запускаю pytest",
    "python": "Запускаю Python",
    "pip": "Работаю с pip",
    "git": "Выполняю git",
    "run_terminal_command": "Выполняю команду",
    "terminal_execute": "Запускаю команду в терминале",
}


class _ProgressLine(QtWidgets.QLabel):
    def __init__(self):
        super().__init__()
        self._line = ""
        self._elide = QtCore.Qt.TextElideMode.ElideRight
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWordWrap(False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)

    def set_progress(self, text, *, latest=False):
        self._line = " ".join(text.split())
        self._elide = (QtCore.Qt.TextElideMode.ElideLeft if latest else QtCore.Qt.TextElideMode.ElideRight)
        self.setAccessibleDescription(self._line)
        self._refresh()

    def _refresh(self):
        visible = self.fontMetrics().elidedText(self._line, self._elide, self.contentsRect().width())
        if visible != self.text():
            self.setText(visible)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()


class AgentTeamTab(QtWidgets.QWidget):
    agent_updated = QtCore.pyqtSignal(int, str, str, str)
    report_updated = QtCore.pyqtSignal(int, str)
    action_logged = QtCore.pyqtSignal(int, str)
    reasoning_received = QtCore.pyqtSignal(object)
    usage_received = QtCore.pyqtSignal(object)
    relationship_updated = QtCore.pyqtSignal(int, int, object)

    def __init__(self, agent_panel, parent=None):
        super().__init__(parent)
        self.setObjectName("agentWorkspace")
        self.setProperty("pyscribeSpecial", "agentTeam")
        self._agent_panel = agent_panel
        self._cancel = None
        self._goal = ""
        self._root = None
        self._question = ""
        self._roles = {}
        self._statuses = {}
        self._logs = {}
        self._journal_lines = []
        self._journal_view = None
        self._reasoning_buffer = ""
        self._reasoning_agent = None
        self._live_tokens = 0
        self._edits = []
        self._applying = None
        self._pending = []
        self._last_result = ""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.split = split
        split.setObjectName("agentWorkspaceSplit")
        split.setHandleWidth(8)
        split.setChildrenCollapsible(False)
        split.addWidget(agent_panel)
        agent_panel.show()
        self.idea = agent_panel.input
        self.idea.setPlaceholderText("Задача для агента")
        sidebar = QtWidgets.QWidget()
        sidebar.setObjectName("agentWorkspaceSidebar")
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        details = QtWidgets.QFrame()
        details.setObjectName("agentWorkspacePanel")
        right = QtWidgets.QVBoxLayout(details)
        right.setContentsMargins(14, 12, 14, 12)
        right.setSpacing(10)

        controls = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Ход работы")
        title.setObjectName("agentWorkspaceTitle")
        controls.addWidget(title)
        controls.addStretch()
        self.journal_button = QtWidgets.QPushButton("Журнал")
        self.journal_button.setObjectName("agentJournalButton")
        self.journal_button.setEnabled(False)
        self.journal_button.clicked.connect(self._show_current_journal)
        controls.addWidget(self.journal_button)
        self.stop_button = QtWidgets.QPushButton("Стоп")
        self.stop_button.setObjectName("agentStopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.stop_button)
        right.addLayout(controls)
        progress = QtWidgets.QHBoxLayout()
        progress.setContentsMargins(3, 4, 3, 4)
        progress.setSpacing(8)
        dot = QtWidgets.QFrame()
        dot.setObjectName("agentProgressDot")
        dot.setFixedSize(6, 6)
        progress.addWidget(dot)
        self.activity = _ProgressLine()
        self.activity.setObjectName("agentWorkspaceActivity")
        self.activity.set_progress("Готов к задаче")
        progress.addWidget(self.activity, 1)
        right.addLayout(progress)
        self._reasoning_timer = QtCore.QTimer(self)
        self._reasoning_timer.setSingleShot(True)
        self._reasoning_timer.setInterval(80)
        self._reasoning_timer.timeout.connect(self._refresh_reasoning)
        self.pending_list = QtWidgets.QListWidget()
        self.pending_list.setObjectName("agentTaskQueue")
        self.pending_list.setMaximumHeight(90)
        self.pending_list.setToolTip("Двойной клик удаляет задачу из очереди")
        self.pending_list.itemDoubleClicked.connect(self._remove_pending)
        self.pending_list.hide()
        self.stats = QtWidgets.QLabel()
        self.stats.setObjectName("agentWorkspaceStats")
        self.stats.setWordWrap(True)
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
        right.addWidget(self.pending_list)
        self.planner = AgentPlanner(self)
        right.addWidget(self.planner)
        sidebar_layout.addWidget(details)
        sidebar_layout.addStretch()
        split.addWidget(sidebar)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([730, 310])
        layout.addWidget(split)
        self.agent_updated.connect(self._update_agent, QtCore.Qt.ConnectionType.QueuedConnection)
        self.report_updated.connect(self._update_report, QtCore.Qt.ConnectionType.QueuedConnection)
        self.action_logged.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
        self.reasoning_received.connect(self._receive_reasoning, QtCore.Qt.ConnectionType.QueuedConnection)
        self.usage_received.connect(self._update_live_usage, QtCore.Qt.ConnectionType.QueuedConnection)
        self.relationship_updated.connect(self._relationship, QtCore.Qt.ConnectionType.QueuedConnection)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "split"):
            direction = (QtCore.Qt.Orientation.Vertical if self.width() < 720
                         else QtCore.Qt.Orientation.Horizontal)
            if self.split.orientation() != direction:
                self.split.setOrientation(direction)
                sizes = [730, 310] if direction == QtCore.Qt.Orientation.Horizontal else [500, 180]
                self.split.setSizes(sizes)

    @QtCore.pyqtSlot(int, int, object)
    def _relationship(self, number, parent, dependencies):
        self._log(number, f"Родитель: {parent}. Зависимости: {dependencies}")

    @property
    def busy(self):
        return self._cancel is not None or self._applying is not None

    @QtCore.pyqtSlot(int, str, str, str)
    def _update_agent(self, number, role, goal, status):
        self._roles[number] = role
        if self._statuses.get(number) == status:
            return
        self._statuses[number] = status
        self._log(number, f"{role} · {status}\n{goal}")
        if number == 0 and self._cancel is not None and not self._cancel.is_set():
            phase = {"Запуск": "Запускаю задачу…", "Координирует": "Готовлю запрос…"}
            self._set_phase(phase.get(status, status))

    def _set_phase(self, text):
        self._reasoning_timer.stop()
        self._reasoning_buffer = ""
        self._reasoning_agent = None
        self.activity.set_progress(text)

    def enqueue(self, text, *, attachment_paths=()):
        root = self._agent_panel._project_root()
        if root != self._root or not root:
            self._agent_panel._append_notice("Проект сменился. Дождитесь завершения текущего запуска.", "warn")
            return False
        options = {"attachment_paths": attachment_paths} if attachment_paths else {}
        file_context = getattr(self._agent_panel, "_team_file_context", None)
        if not attachment_paths and callable(file_context):
            options["file_context"] = file_context()
        self._pending.append((root, text, options))
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
        root, text, options = self._pending[0]
        if self._agent_panel._project_root() != root:
            self._pending.clear()
            self.pending_list.clear()
            self.pending_list.hide()
            return
        if self.submit(text, **options):
            self._pending.pop(0)
            self.pending_list.takeItem(0)
            self.pending_list.setVisible(bool(self._pending))

    @QtCore.pyqtSlot(int, str)
    def _update_report(self, number, text):
        self._log(number, text)

    @QtCore.pyqtSlot(int, str)
    def _log(self, number, text):
        excerpt = clip_utf8(str(text), 8000)
        if excerpt != text:
            excerpt += "\n… [Запись журнала сокращена.]"
        entry = QtCore.QTime.currentTime().toString("HH:mm:ss") + "  " + excerpt
        self._logs.setdefault(number, []).append(entry)
        self._journal_lines.append(entry)
        self.journal_button.setEnabled(True)
        if self._journal_view is not None:
            try:
                self._journal_view.appendPlainText(entry)
            except RuntimeError:
                self._journal_view = None

        name, _, arguments = excerpt.partition("\n")
        label = TOOL_PROGRESS.get(name)
        if label and self._cancel is not None and not self._cancel.is_set():
            try:
                args = json.loads(arguments)
            except ValueError:
                args = {}
            target = ""
            if isinstance(args, dict):
                target = args.get("path") or args.get("query") or args.get("target") or ""
            self._set_phase(f"{label} · {clip_utf8(str(target), 240)}" if target else label)

    @QtCore.pyqtSlot(object)
    def _receive_reasoning(self, flush):
        flush()

    def _append_reasoning(self, number, piece):
        if self._cancel is None or self._cancel.is_set() or not piece:
            return
        if number != self._reasoning_agent:
            self._reasoning_buffer = ""
            self._reasoning_agent = number
        self._reasoning_buffer = (self._reasoning_buffer + piece)[-1200:]
        if not self._reasoning_timer.isActive():
            self._reasoning_timer.start()

    def _refresh_reasoning(self):
        if self._cancel is None or self._cancel.is_set():
            return
        parts = [part.strip() for part in REASONING_BREAK.split(self._reasoning_buffer) if part.strip()]
        if parts:
            current = parts[-1]
            if len(parts) > 1 and len(current) < 12 and current[-1] not in ".!?。！？":
                current = parts[-2]
            self.activity.set_progress(current, latest=True)

    @QtCore.pyqtSlot(object)
    def _update_live_usage(self, usage):
        if self._cancel is None or not isinstance(usage, dict):
            return
        self._live_tokens += int(total_tokens(usage) or 0)
        self.stats.setText(f"Токены: {self._live_tokens:,}".replace(",", " "))

    def _show_current_journal(self):
        from PyQt6 import sip

        window = self._agent_panel.main_window
        view = self._journal_view
        if view is not None and not sip.isdeleted(view):
            group, index = window.find_tab_for_widget(view)
            if group is not None:
                window._set_active_group(group)
                group.setCurrentIndex(index)
                return
        self._journal_view = self._journal_tab("ИИ-агент", "\n\n".join(self._journal_lines))

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
                (n, self._roles.get(n, "ИИ-агент"), "\n\n".join(entries))
                for n, entries in self._logs.items()])

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

    def submit(self, text, *, scheduled_role=None, attachment_paths=(), file_context=None):
        if self.busy:
            return False
        if not text:
            return
        access_mode = getattr(self._agent_panel, "_access_mode", lambda: "full")()
        if attachment_paths and access_mode == "write_only":
            self._agent_panel._append_notice("Включите чтение для отправки вложений из очереди.", "warn")
            return False
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
        toolbox = self._agent_panel._build_local_tools(cancel.is_set, background=True)
        if "read_file" not in {tool["function"]["name"] for tool in toolbox.definitions()}:
            self._agent_panel._append_notice("Включите чтение файлов проекта в разрешениях агента.", "warn")
            return False
        self._root = root
        self._run_id = uuid4().hex
        toolbox.team_cache = self.planner.cache
        toolbox.team_run_id = self._run_id
        self._goal = (self._goal + "\n\nУточнение: " + self._question + "\nОтвет: " + text
                      if self._question else text)
        self._question = ""
        self._cancel = cancel
        self.stop_button.setEnabled(True)
        self.stats.clear()
        self._live_tokens = 0
        self.applied_button.hide()
        self._set_edits([])
        self._roles.clear()
        self._statuses.clear()
        self._logs.clear()
        self._journal_lines.clear()
        self._journal_view = None
        self._update_agent(0, "Руководитель", self._goal, "Запуск")
        self._agent_panel._record_team_message("user", text)
        options = {"attachment_paths": attachment_paths} if attachment_paths else {}
        if not attachment_paths:
            collect_file_context = getattr(self._agent_panel, "_team_file_context", None)
            if file_context is None and callable(collect_file_context):
                file_context = collect_file_context()
            options.update(file_context or {})
        from services.ai_service import AIService
        if isinstance(service, AIService):
            service.config["ai_team_strategy"] = "single"  # three-stage team is paused
        if self._last_result:
            options["previous_result"] = self._last_result
        server_configs = getattr(self._agent_panel, "_mcp_server_configs", None)
        if access_mode == "full" and server_configs is not None:
            configs = server_configs()
            if configs:
                options["mcp_servers"] = configs
        preview_lock = Lock()
        previews = {}

        def show_phase(number, role, goal, status):
            with preview_lock:
                previews.pop(number, None)
            self.agent_updated.emit(number, role, goal, status)

        def show_reasoning(number, piece):
            with preview_lock:
                batch = previews.get(number)
                if batch is not None:
                    batch[0] = (batch[0] + piece[-1200:])[-1200:]
                    return
                batch = previews[number] = [piece[-1200:]]

            def flush():
                with preview_lock:
                    if previews.get(number) is batch:
                        del previews[number]
                if self._cancel is cancel:
                    self._append_reasoning(number, batch[0])

            self.reasoning_received.emit(flush)

        show_reasoning_enabled = getattr(self._agent_panel, "_show_reasoning", lambda: True)()
        run_async(partial(run_team, service, toolbox, self._goal, show_phase, cancel.is_set,
                          report=self.report_updated.emit, trace=self.action_logged.emit,
                          reasoning=show_reasoning if show_reasoning_enabled else None,
                          usage_progress=self.usage_received.emit,
                          scheduled_role=scheduled_role, relationship=self.relationship_updated.emit, **options),
                  on_done=self._finish, on_error=self._finish)
        return True

    def stop(self):
        self._pending.clear()
        self.pending_list.clear()
        self.pending_list.hide()
        self.planner.finished(False)
        if self._cancel is not None:
            self._cancel.set()
            self.stop_button.setEnabled(False)
            self._set_phase("Останавливаю задачу…")
            self._log(0, "Остановка запрошена пользователем.")
            self._archive()
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
        dialog.setWindowTitle("Предложенные правки")
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
        discard = buttons.addButton("Отклонить правку", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(dialog.reject)
        apply.clicked.connect(dialog.accept)
        discard.clicked.connect(lambda: dialog.done(2))
        layout.addWidget(buttons)
        decision = dialog.exec()
        if decision == 2:
            edit = edits[files.currentIndex()]
            self._log(0, f"Пользователь отклонил правку: {edit.path}")
            self._archive()
            self._set_edits([pending for pending in self._edits if pending is not edit])
            dialog.deleteLater()
            QtCore.QTimer.singleShot(0, self._next_pending)
            return
        if decision != QtWidgets.QDialog.DialogCode.Accepted:
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
            self._set_phase("Остановлено")
            self._archive()
            if usage_recorded:
                self._persist_chat_session()
            return
        if isinstance(result, str):
            if 0 in self._roles:
                self._update_agent(0, self._roles[0], self._goal, "Ошибка")
                self._update_report(0, result)
                self._archive()
            self._agent_panel._append_notice(result, "warn")
            self._set_phase("Ошибка · очередь ожидает решения")
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
        self._set_phase("Нужен ваш ответ" if result.question else
                        "Есть незавершённая работа" if not result.complete else
                        "Изменения готовы к просмотру" if result.edits else "Задача завершена")
        if result.question:
            self.idea.setFocus()
        usage = result.usage
        total = total_tokens(usage)
        incoming = input_tokens(usage)
        outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
        if incoming is not None:
            parts = [f"Вход {incoming:,}"]
            if "cache_read_input_tokens" in usage:
                cached = usage["cache_read_input_tokens"]
                parts.append(f"из кэша {cached:,}" + (f" ({cached / incoming:.1%})" if incoming else ""))
            if outgoing is not None:
                parts.append(f"выход {outgoing:,}")
            self.stats.setText(" · ".join(parts).replace(",", " "))
        else:
            self.stats.setText(f"Токены: {total}" if total is not None else "Расход токенов недоступен")
        if result.complete and not result.failed_agents:
            QtCore.QTimer.singleShot(0, self._next_pending)
