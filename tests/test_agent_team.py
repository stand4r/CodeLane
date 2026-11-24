import json
from threading import Barrier, Event

from services import agent_team

_MENU_TEST_APP = None


class Toolbox:
    def definitions(self):
        return []


class Supervisor:
    last_usage = {"total_tokens": 10}

    def __init__(self, script):
        self.script = script

    def converse(self, prompt, *, tools, tool_runner, should_stop, max_rounds=None, unlimited_rounds=False):
        return self.script(tool_runner, should_stop)


def test_supervisor_uses_mcp_and_attachment_without_delegation(monkeypatch, tmp_path):
    from services.mcp_client import MCPClient, MCPServerConfig, MCPTool

    path = tmp_path / "notes.md"
    tool = MCPTool(server="docs", name="lookup")
    closed = []

    def connect(client):
        client.tools = [tool]
        client.capabilities = {"resources": {}}

    monkeypatch.setattr(MCPClient, "connect", connect)
    monkeypatch.setattr(MCPClient, "call_tool", lambda client, name, args: args["query"])
    monkeypatch.setattr(MCPClient, "close", lambda client: closed.append(client.config.name))

    class Lead:
        last_usage = {}

        def converse(self, _prompt, *, tools, tool_runner, attachment_paths, **_options):
            assert attachment_paths == (path,)
            names = {spec["function"]["name"] for spec in tools}
            assert {tool.full_name, "mcp_list_resources", "mcp_read_resource"} <= names
            return tool_runner(tool.full_name, {"query": "Found"})

    result = agent_team.run_team(Lead(), Toolbox(), "Read notes", lambda *_: None, Event().is_set,
                                 attachment_paths=(path,), mcp_servers=[MCPServerConfig(name="docs")])
    assert result.complete and result.answer == "Found"
    assert closed == ["docs"]


def test_team_continues_without_a_round_budget(monkeypatch, tmp_path):
    from services.agent_tools import LocalToolbox
    from services.ai_service import AIService, ToolCall

    (tmp_path / "demo.py").write_text("value = 42", encoding="utf-8")
    service = AIService(config={"ai_agent_round_budget": 1})
    monkeypatch.setattr(service, "configuration_problem", lambda: None)
    calls = []

    def model_round(*_args):
        calls.append(True)
        if len(calls) == 1:
            return "Прочитаю файл", [ToolCall("read", "read_file", {"path": "demo.py"})]
        return "Готово", []

    monkeypatch.setattr(service, "_openai_tool_round", model_round)
    result = agent_team.run_team(service, LocalToolbox(str(tmp_path)), "Проверка",
                                 lambda *_: None, Event().is_set)
    assert len(calls) == 2
    assert result.complete
    assert result.answer == "Готово"


def test_supervisor_assigns_roles_and_collects_parallel_results(monkeypatch):
    ready = Barrier(3)
    events = []

    def delegate(service, toolbox, definitions, args, stopped, on_usage, **kwargs):
        ready.wait(timeout=3)
        on_usage({"total_tokens": 2})
        return json.dumps({"role": args["role"], "result": args["task"]})

    monkeypatch.setattr(agent_team, "delegate", delegate)

    def script(route, stopped):
        tasks = [{"role": role, "task": role} for role in ("design", "code", "test")]
        reports = json.loads(route("assign_team", {"tasks": tasks}))
        assert [report["result"] for report in reports] == ["design", "code", "test"]
        assert "повторные" in route("assign_team", {"tasks": tasks})
        return "Согласованный результат"

    result = agent_team.run_team(Supervisor(script), Toolbox(), "Идея",
                                 lambda *event: events.append(event), Event().is_set)
    assert result.answer == "Согласованный результат"
    assert result.usage["total_tokens"] == 16
    assert [event[1] for event in events if event[0] == 0] == ["Руководитель"] * 2
    assert sorted(event[0] for event in events if event[3] == "Готово") == [0, 1, 2, 3]


def test_supervisor_pauses_for_a_clarification(monkeypatch):
    monkeypatch.setattr(agent_team, "delegate", lambda *args, **kwargs:
                        json.dumps({"role": "requirements", "result": "Нужны критерии"}))

    def script(route, stopped):
        route("assign_team", {"tasks": [{"role": "requirements", "task": "Уточнить"}]})
        route("ask_user", {"question": "Какой размер проекта считать большим?"})
        assert stopped()
        return ""

    result = agent_team.run_team(Supervisor(script), Toolbox(), "Идея",
                                 lambda *event: None, Event().is_set)
    assert result.question == "Какой размер проекта считать большим?"


def test_team_tab_displays_worker_updates(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from PyQt6 import QtCore, QtWidgets
    from widgets.AIAgentPanel import AIAgentPanel
    from widgets.AgentTeamTab import AgentTeamTab

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr("widgets.AgentPlanner.AgentPlanner.cache_dir", staticmethod(lambda: tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    loop = QtCore.QEventLoop()
    toolbox = SimpleNamespace(definitions=lambda: [{"function": {"name": "read_file"}}])
    owner = QtWidgets.QWidget()
    owner.input = QtWidgets.QPlainTextEdit(owner)
    owner._project_root = lambda: "project"
    owner._worker = None
    owner._service = lambda: SimpleNamespace(configuration_problem=lambda: None)
    owner._build_local_tools = lambda stopped, **kwargs: toolbox
    transcript = []

    def record(role, text):
        transcript.append((role, text))
        if role == "assistant":
            loop.quit()

    owner._record_team_message = record

    def work(service, tools, goal, emit, stopped, *, report, trace, scheduled_role, relationship,
             reasoning, usage_progress):
        emit(0, "Руководитель", goal, "Готово")
        reasoning(0, "Проверяю изменения")
        usage_progress({"total_tokens": 20})
        emit(1, "Тестировщик", "Проверить изменения", "Готово")
        trace(1, "read_file: demo.py")
        report(1, "Результат проверки")
        return agent_team.TeamResult(answer="Отчёт", usage={"total_tokens": 42})

    monkeypatch.setattr("widgets.AgentTeamTab.run_team", work)
    group = QtWidgets.QTabWidget()
    tab = AgentTeamTab(owner, group)
    index = group.addTab(tab, "ИИ-агент")
    group.resize(1040, 620)
    group.show()
    owner.main_window = SimpleNamespace(_find_special_tab=lambda kind: (group, index), active_tab_widget=group)
    assert tab.team_mode.isChecked()
    owner.input.setPlainText("Проверить проект")
    AIAgentPanel.send_message(owner)
    assert owner.input.toPlainText() == ""
    assert 0 in tab._cards
    QtCore.QTimer.singleShot(3000, loop.quit)
    loop.exec()
    app.processEvents()
    assert transcript == [("user", "Проверить проект"), ("assistant", "Отчёт")]
    assert "Проверяю изменения" in tab.reasoning_view.text()
    assert set(tab._cards) == {0, 1}
    assert tab.graph.nodes[1] is tab._cards[1]
    assert len(tab.graph.edges) == 1
    assert "Проверить изменения" in tab._cards[1].toolTip()
    assert tab._cards[1].report == "Результат проверки"
    tab._show_report(tab._cards[1])
    assert "read_file: demo.py" in group.currentWidget().toPlainText()
    assert tab.planner.cache.journal("project", tab._run_id, 1).endswith("Результат проверки")


    assert not tab.graph.grab().isNull()
    assert tab.stats.text() == "Токены: 42"
    assert not tab.stop_button.isEnabled()
    assert tab.activity.text() == "Задача завершена"
    assert tab.enqueue("Следующая задача")
    assert tab.pending_list.count() == 1
    accepted = []
    monkeypatch.setattr(tab, "submit", lambda text: accepted.append(text) or True)
    tab._question = "Уточните"
    tab._next_pending()
    assert not accepted
    tab._question = ""
    tab._next_pending()
    assert accepted == ["Следующая задача"]
    assert not tab._pending
    assert tab.enqueue("Отменить эту задачу")
    tab.stop()
    assert not tab._pending and tab.pending_list.count() == 0
    assert tab.enqueue("Другой проект")
    owner._project_root = lambda: "other-project"
    tab._next_pending()
    assert not tab._pending


def test_chat_header_menu_opens_team_archives(monkeypatch, tmp_path):
    global _MENU_TEST_APP
    from pathlib import Path
    from types import SimpleNamespace
    from PyQt6 import QtWidgets
    from utils.settings_manager import init_settings_manager
    from widgets.AIAgentPanel import AIAgentPanel

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _MENU_TEST_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    init_settings_manager(str(Path(__file__).resolve().parents[1]))
    panel = AIAgentPanel()
    calls = []
    team = SimpleNamespace(show_journals=lambda: calls.append("journals"),
                           show_snapshots=lambda: calls.append("snapshots"))
    group = SimpleNamespace(widget=lambda _index: team)
    panel.main_window = SimpleNamespace(_find_special_tab=lambda _kind: (group, 0))
    monkeypatch.setattr(panel, "_open_agent_team", lambda **_kwargs: None)
    monkeypatch.setattr(panel, "_project_root", lambda: str(tmp_path))
    menu = panel._build_context_menu()
    actions = {action.text(): action for action in menu.actions()}
    assert "Журналы команды…" in actions and "Снимки команды…" in actions
    actions["Журналы команды…"].trigger()
    actions["Снимки команды…"].trigger()
    assert calls == ["journals", "snapshots"]
    menu.deleteLater()
    panel.deleteLater()
    _MENU_TEST_APP.processEvents()


def test_team_finish_accounts_usage_in_chat_session(monkeypatch):
    from PyQt6 import QtWidgets

    from services.agent_team import TeamResult
    from widgets.AgentTeamTab import AgentTeamTab

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calls = []

    class Panel(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.input = QtWidgets.QPlainTextEdit(self)

        def _record_session_stats(self, usage):
            calls.append(("usage", dict(usage)))

        def _record_team_message(self, role, text):
            calls.append(("message", role))

        def save_chat_session(self):
            calls.append(("save",))

        def _append_notice(self, text, kind="info"):
            calls.append(("notice", kind))

    group = QtWidgets.QTabWidget()
    tab = AgentTeamTab(Panel(), group)
    group.addTab(tab, "ИИ-агент")
    tab._goal = "Проверить проект"
    tab._cancel = Event()

    tab._finish(TeamResult(answer="Отчёт", usage={"total_tokens": 42}))
    # Usage is accounted before the assistant message stores the chat session.
    assert calls == [("usage", {"total_tokens": 42}), ("message", "assistant")]
    assert tab.stats.text() == "Токены: 42"

    calls.clear()
    tab._cancel = Event()
    tab._finish(TeamResult(answer="Без отчёта провайдера"))
    assert calls == [("message", "assistant")]

    calls.clear()
    cancel = Event()
    cancel.set()
    tab._cancel = cancel
    tab._finish(TeamResult(answer="Отчёт", usage={"total_tokens": 7}))
    assert calls == [("usage", {"total_tokens": 7}), ("save",)]

    calls.clear()
    tab._cancel = Event()
    tab._finish("Провайдер недоступен")
    assert [call[0] for call in calls] == ["notice"]


def test_team_run_usage_lands_in_chat_session_counter(monkeypatch, tmp_path):
    """T3: расход команды попадает в счётчик диалога чата, а не только в tab.stats."""
    from pathlib import Path

    from PyQt6 import QtWidgets
    from utils.settings_manager import init_settings_manager
    from services.agent_team import TeamResult
    from widgets.AIAgentPanel import AIAgentPanel
    from widgets.AgentTeamTab import AgentTeamTab

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    init_settings_manager(str(Path(__file__).resolve().parent.parent))
    monkeypatch.setattr("widgets.AgentPlanner.AgentPlanner.cache_dir", staticmethod(lambda: tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    panel = AIAgentPanel(None)
    try:
        notices = []
        panel._append_notice = lambda text, level="info": notices.append((level, text))
        group = QtWidgets.QTabWidget()
        tab = AgentTeamTab(panel, group)
        group.addTab(tab, "Команда")
        group.resize(1040, 620)
        group.show()
        tab._root = "project"
        tab._run_id = "run"
        tab._goal = "Проверить проект"

        panel._record_session_stats({"total_tokens": 5})
        assert panel._session_stats["total"] == 5
        tab._cancel = Event()
        tab._finish(TeamResult(answer="Отчёт", usage={"total_tokens": 42}))
        assert panel._session_stats["total"] == 47, panel._session_stats
        assert panel._session_stats["measured"] == 2
        assert panel._session_stats["unreported"] == 0
        line = panel.stats_label.text()
        assert "запрос 42" in line and "диалог 47" in line, line
        assert tab.stats.text() == "Токены: 42"
        assert tab._cancel is None

        # Пустой usage не должен ни увеличивать unreported, ни ломать счётчик.
        tab._cancel = Event()
        tab._finish(TeamResult(answer="Второй отчёт", usage={}))
        assert panel._session_stats["total"] == 47, panel._session_stats
        assert panel._session_stats["unreported"] == 0, panel._session_stats
        assert "диалог 47" in panel.stats_label.text()
        assert not notices
        app.processEvents()
    finally:
        panel.deleteLater()


def test_empty_delegate_is_counted_and_supervisor_can_read_directly(tmp_path):
    from services.agent_tools import LocalToolbox

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, tool_runner, should_stop, max_rounds=None, unlimited_rounds=False):
            if prompt.startswith("Роль:"):
                return "Проверить код" if "Уточнить" in prompt else " "
            tool_runner("assign_team", {"tasks": [{"role": "requirements", "task": "Уточнить"}]})
            result = json.loads(tool_runner("assign_team", {"tasks": [
                {"role": "code", "task": "Исследовать код"},
                {"role": "test", "task": "Изучить проверки"},
            ]}))
            assert all("не вернул" in item["error"] for item in result["results"])
            assert result["parallelism"] is None
            return tool_runner("read_file", {"path": "app.py"})

    (tmp_path / "app.py").write_text("value = 42", encoding="utf-8")
    events = []
    result = agent_team.run_team(Provider(), LocalToolbox(str(tmp_path)), "Разобрать проект",
                                 lambda *event: events.append(event), Event().is_set)
    assert result.failed_agents == 2
    assert "value = 42" in result.answer
    assert sorted(event[0] for event in events if event[3] == "Ошибка") == [2, 3]


def test_supervisor_incomplete_answer_preserves_successes_and_reports_errors(monkeypatch):
    events = []

    def delegate(service, toolbox, definitions, args, stopped, on_usage, **kwargs):
        if args["role"] in {"requirements", "test"}:
            raise RuntimeError("Вернулся пустой ответ.")
        return json.dumps({"role": args["role"], "result": "Найдена причина."})

    monkeypatch.setattr(agent_team, "delegate", delegate)

    def script(route, stopped):
        batch = [{"role": role, "task": role} for role in ("requirements", "test", "code")]
        result = json.loads(route("assign_team", {"tasks": batch}))
        assert len(result["results"]) == 3
        assert not result["requirements_ready"]
        return ""

    result = agent_team.run_team(Supervisor(script), Toolbox(), "Идея",
                                 lambda *event: events.append(event), Event().is_set)
    assert result.failed_agents == 2
    assert "Частичный итог" in result.answer
    assert "Найдена причина." in result.answer
    assert "Вернулся пустой ответ." in result.answer
    assert events[-1][3] == "Частичный итог"


def test_nested_error_is_reported_even_when_supervisor_says_done(monkeypatch):
    states = []

    def fake_delegate(service, toolbox, definitions, args, stopped, on_usage, **options):
        if args["task"] == "parent":
            children = options["assign_children"]([{"role": "test", "task": "child"}])
            assert "ошиб" in children[0]["error"]
        if args["task"] == "child":
            raise RuntimeError("Субагент завершился с ошибкой.")
        return json.dumps({"role": args["role"], "result": "Готово"})

    monkeypatch.setattr(agent_team, "delegate", fake_delegate)

    def script(route, stopped):
        route("assign_team", {"tasks": [
            {"role": "requirements", "task": "criteria"},
            {"role": "code", "task": "parent"},
        ]})
        return "Всё готово"

    result = agent_team.run_team(Supervisor(script), Toolbox(), "Task",
                                 lambda *state: states.append(state), Event().is_set, parallelism=1)
    assert not result.complete and result.failed_agents == 1
    assert "Частичный итог" in result.answer
    assert "завершился с ошибкой" in result.answer
    assert states[-1][-1] == "Частичный итог"


def test_long_dependency_result_does_not_invalidate_dependent_task(tmp_path):
    from services.agent_tools import LocalToolbox

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, tool_runner, should_stop, max_rounds=None, unlimited_rounds=False):
            if prompt.startswith("Роль:"):
                if "dependent" in prompt:
                    assert "Результаты зависимостей" in prompt
                    assert "x" * 4001 in prompt
                    return "dependent finished"
                return "x" * 5000
            results = json.loads(tool_runner("assign_team", {"tasks": [
                {"key": "requirements", "role": "requirements", "task": "criteria"},
                {"key": "dependent", "role": "test", "task": "dependent", "depends_on": ["requirements"]},
            ]}))
            assert results[1]["result"] == "dependent finished"
            return "Готово"

    result = agent_team.run_team(Provider(), LocalToolbox(str(tmp_path)), "Task",
                                 lambda *_: None, Event().is_set)
    assert result.complete and result.failed_agents == 0


def test_subagent_empty_result_preserves_its_proposals(tmp_path):
    from services.agent_delegation import delegate
    from services.agent_tools import LocalToolbox

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, tool_runner, should_stop, max_rounds=None, unlimited_rounds=False):
            tool_runner("propose_edit", {"path": "file.py", "old_text": "a", "new_text": "b"})
            self.last_stop_reason = "empty"
            return ""

    result = json.loads(delegate(Provider(), LocalToolbox(str(tmp_path)), [], {"role": "code", "task": "edit"},
                                 lambda: False, collect_edits=True))
    assert "не вернул итоговый ответ" in result["error"]
    assert result["proposed_edits"][0]["new_text"] == "b"


def test_team_agent_can_use_editor_and_terminal_tools_without_round_limit(tmp_path):
    from services.agent_tools import LocalToolbox
    from services.team_cache import TeamCache

    (tmp_path / "demo.txt").write_text("before", encoding="utf-8")

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, tool_runner, should_stop, unlimited_rounds=False, **kwargs):
            assert unlimited_rounds
            names = {tool["function"]["name"] for tool in tools}
            if prompt.startswith("Роль:"):
                if "Постановка задачи" in prompt:
                    return "Критерии готовы"
                assert {"edit_file", "run_terminal_command"} <= names
                assert "before" in tool_runner("read_file", {"path": "demo.txt"})
                assert "Изменён" in tool_runner("edit_file", {
                    "path": "demo.txt", "old_text": "before", "new_text": "after"})
                return "Файл изменён"
            tool_runner("assign_team", {"tasks": [
                {"role": "requirements", "task": "Критерии"},
                {"role": "code", "task": "Обновить файл"},
            ]})
            return "Готово"

    box = LocalToolbox(str(tmp_path), access_mode="full")
    box.team_cache = TeamCache(tmp_path / "cache")
    box.team_run_id = "run"
    result = agent_team.run_team(Provider(), box,
                                 "Изменить файл", lambda *_: None, Event().is_set)
    assert result.complete
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "after"
    assert result.changed_files == ["demo.txt"]
    box.team_cache.restore_run(str(tmp_path), "run", box)
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "before"


def test_supervisor_can_edit_external_file_in_full_mode(tmp_path):
    from services.agent_tools import LocalToolbox
    from services.team_cache import TeamCache

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("before", encoding="utf-8")

    class Provider:
        last_usage = {}

        def converse(self, _prompt, *, tool_runner, **_kwargs):
            assert "Изменён" in tool_runner("edit_file", {
                "path": str(external), "old_text": "before", "new_text": "after"})
            return "Готово"

    box = LocalToolbox(str(project), access_mode="full")
    box.team_cache = TeamCache(tmp_path / "cache")
    result = agent_team.run_team(Provider(), box, "Edit external file",
                                 lambda *_: None, Event().is_set)
    assert result.complete
    assert external.read_text(encoding="utf-8") == "after"


def test_worker_receives_role_tools_even_with_nested_delegation(tmp_path):
    from services.agent_delegation import delegate
    from services.agent_tools import LocalToolbox

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, **_kwargs):
            names = {item["function"]["name"] for item in tools}
            assert {"read_file", "assign_team"} <= names
            assert "run_admin_command" not in names
            assert "edit_file" not in names
            assert "capture_screen" not in names
            return "Критерии готовы"

    box = LocalToolbox(str(tmp_path), access_mode="full")
    delegate(Provider(), box, box.definitions(), {"role": "requirements", "task": "Критерии"},
             lambda: False, assign_children=lambda _tasks: [])


def test_team_collects_and_checks_proposals_before_explicit_apply(tmp_path):
    import pytest
    from services.agent_tools import LocalToolbox, ToolError

    path = tmp_path / "demo.py"
    initial = "left = 1\nright = 2\n"
    path.write_text(initial, encoding="utf-8")
    toolbox = LocalToolbox(str(tmp_path))

    class Provider:
        last_usage = {}

        def converse(self, prompt, *, tools, tool_runner, should_stop, max_rounds=None, unlimited_rounds=False):
            if prompt.startswith("Роль:"):
                if "Левая" in prompt:
                    tool_runner("propose_edit", {"path": "demo.py", "old_text": "left = 1", "new_text": "left = 4"})
                if "Правая" in prompt:
                    tool_runner("propose_edit", {"path": "demo.py", "old_text": "right = 2", "new_text": "right = 8"})
                return "Отчёт субагента"
            tool_runner("assign_team", {"tasks": [{"role": "requirements", "task": "Уточнить"}]})
            reports = json.loads(tool_runner("assign_team", {"tasks": [
                {"role": "code", "task": "Левая часть"}, {"role": "code", "task": "Правая часть"},
            ]}))
            for report in reports:
                assert "добавлена" in tool_runner("accept_proposal", {"id": report["proposal_ids"][0]})
            assert "пересекается" in tool_runner("propose_edit", {
                "path": "./demo.py", "old_text": "left = 1\nright = 2", "new_text": "other = 0"})
            self.last_stop_reason = "empty"
            return ""

    result = agent_team.run_team(Provider(), toolbox, "Изменить проект",
                                 lambda *_: None, Event().is_set)
    edit, = result.edits
    assert "пустой ответ" in result.answer
    assert "+right = 8" in edit.diff()
    assert path.read_text(encoding="utf-8") == initial
    path.write_text(initial + "newer = True\n", encoding="utf-8")
    with pytest.raises(ToolError, match="изменился"):
        edit.apply(toolbox)
    assert "newer = True" in path.read_text(encoding="utf-8")
    path.write_text(initial, encoding="utf-8")
    edit.apply(toolbox)
    assert path.read_text(encoding="utf-8") == "left = 4\nright = 8\n"


def test_candidate_preserves_a_newer_unsaved_buffer(tmp_path):
    import pytest
    from services.agent_edits import EditCandidate
    from services.agent_tools import LocalToolbox, ToolError

    path = tmp_path / "demo.py"
    path.write_text("value = 0\n", encoding="utf-8")
    buffer = "value = 1\n"
    operations = []

    def ui(kind, payload):
        if kind == "read_buffer":
            return buffer
        operations.append((kind, payload))
        return "Изменено во вкладке"

    toolbox = LocalToolbox(str(tmp_path), ui_action=ui)
    candidate = EditCandidate(toolbox)
    candidate.add({"path": "demo.py", "old_text": "value = 1", "new_text": "value = 2"})
    edit, = candidate.files()
    buffer += "draft = True\n"
    with pytest.raises(ToolError, match="изменился"):
        edit.apply(toolbox)
    assert operations == []
    assert path.read_text(encoding="utf-8") == "value = 0\n"
    buffer = "value = 1\n"
    assert edit.apply(toolbox) == "Изменено во вкладке"
    action, payload = operations[0]
    assert action == "edit_file" and payload["expected"] == buffer

def test_team_prompt_excludes_optimizer_history_and_previous_result(monkeypatch):
    from services.team_optimizer import TeamOptimizer
    monkeypatch.setattr(TeamOptimizer, 'recommend', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must be on demand')))
    class Capture:
        last_usage = {}
        def converse(self, prompt, *, tools, tool_runner, **kwargs):
            assert len(prompt) < 700
            assert 'old-private-goal' not in prompt
            assert 'recent_lessons' not in prompt and 'Начальная оценка' not in prompt
            assert 'read_previous_result' in {t['function']['name'] for t in tools}
            assert tool_runner('read_previous_result', {'offset': 0, 'length': 100}) == 'old-private-goal\n'
            return '4'
    result = agent_team.run_team(Capture(), Toolbox(), 'Сколько 2+2?', lambda *_: None, lambda: False,
                                 previous_result='old-private-goal\n')
    assert result.complete and result.answer == '4'


def test_previous_result_tool_is_absent_in_new_team_run():
    class Capture:
        last_usage = {}
        def converse(self, prompt, *, tools, **kwargs):
            assert 'read_previous_result' not in {t['function']['name'] for t in tools}
            return 'done'
    assert agent_team.run_team(Capture(), Toolbox(), 'task', lambda *_: None, lambda: False).complete
