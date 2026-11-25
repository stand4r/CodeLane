"""Regression tests for the AI chat transcript: hover geometry, start scroll, team mode."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

from utils.settings_manager import init_settings_manager

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(ms: int = 200) -> None:
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()
    _APP.processEvents()


def _panel(owner=None):
    try:
        init_settings_manager(str(ROOT))
    except Exception:
        pass
    from widgets.AIAgentPanel import AIAgentPanel

    panel = AIAgentPanel(None)
    panel.main_window = owner
    return panel


def _enter(bubble) -> None:
    point = QtCore.QPointF(1, 1)
    bubble.enterEvent(QtGui.QEnterEvent(point, point, point))


def _leave(bubble) -> None:
    bubble.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))


def test_hover_actions_do_not_change_bubble_geometry():
    """Showing copy/regenerate must not resize the bubble or move the scrollbar."""
    panel = _panel()
    panel.resize(420, 500)
    panel.show()
    _pump()
    bubble = panel._append_assistant("Ответ агента на вопрос\nвторая строка текста")
    _pump()
    height_before = (bubble.height(), bubble.sizeHint().height(), panel.chat.height())
    assert bubble.copy_btn.isVisible() is False
    _enter(bubble)
    _pump(80)
    assert bubble.copy_btn.isVisible() is True, "buttons must appear on hover"
    height_hover = (bubble.height(), bubble.sizeHint().height(), panel.chat.height())
    _leave(bubble)
    _pump(80)
    assert bubble.copy_btn.isVisible() is False
    height_after = (bubble.height(), bubble.sizeHint().height(), panel.chat.height())
    assert height_before == height_hover == height_after, "hover changes the bubble layout"
    panel.close()


def test_restored_session_opens_at_last_message(tmp_path):
    """A restored conversation starts at the end, and manual scrolling wins later."""
    from core.project_state import project_state_file

    messages = []
    for index in range(14):
        messages.append({"role": "user", "content": f"вопрос {index}"})
        messages.append({"role": "assistant", "content": "ответ\n" * 4})
    path = project_state_file(tmp_path, "ai_chat.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2, "active_session_id": "s1",
        "sessions": [{"id": "s1", "title": "Диалог", "created_at": "2024-01-01",
                      "updated_at": "2024-01-02", "messages": messages}]}), encoding="utf-8")

    owner = SimpleNamespace(current_project=SimpleNamespace(root_path=str(tmp_path)))
    panel = _panel(owner)
    panel.resize(420, 420)
    panel.show()
    _pump()
    assert panel.load_chat_session() == len(messages)
    _pump(700)
    bar = panel.transcript.verticalScrollBar()
    assert bar.maximum() > 0, "the transcript must overflow for this check to mean anything"
    assert bar.value() == bar.maximum(), "restored chat must open on the last message"
    bar.setValue(0)  # the user scrolls up by hand
    _pump(300)
    assert bar.value() == 0
    panel._append_assistant("новое сообщение")
    _pump(150)
    assert bar.value() == 0, "a new message must not steal the user's scroll position"
    panel.close()


def test_agent_workspace_uses_a_single_progress_line():
    from widgets.AgentTeamTab import AgentTeamTab
    from widgets.AIAgentPanel import AIAgentPanel

    group = QtWidgets.QTabWidget()
    panel = _panel()
    tab = AgentTeamTab(panel, group)
    group.addTab(tab, "ИИ-агент")
    group.resize(1000, 600)
    group.show()
    _pump(120)
    assert not group.findChildren(QtWidgets.QPushButton, "agentModeButton")
    labels = [button.text() for button in group.findChildren(QtWidgets.QPushButton)]
    assert "Режим: команда" not in labels and "Режим: чат" not in labels
    assert panel.input.placeholderText() == "Задача для агента"
    assert tab.activity.text() == "Готов к задаче"
    assert not tab.activity.wordWrap()
    group.close()
    assert AIAgentPanel is not None


def test_explain_shortcut_keeps_the_team_chat_closed():
    from app.main_window import ModernMainWindow

    editor = object()
    requests = []
    activity = []
    owner = SimpleNamespace(
        extension_enabled=lambda _: True,
        get_current_editor=lambda: editor,
        _handle_activity=activity.append,
        ai_panel=SimpleNamespace(send_with_context=lambda prompt, **kwargs: requests.append(kwargs["editor"])),
    )
    ModernMainWindow.action_ai_explain_selection(owner)
    assert requests == [editor]
    assert activity == []

    ModernMainWindow.show_ai_agent(owner)
    assert activity == ["ai"]


def test_quick_explain_from_team_chat_uses_only_the_selection(monkeypatch, qtbot):
    source = QtWidgets.QPlainTextEdit("selected_code\nother_code")
    qtbot.addWidget(source)
    cursor = source.textCursor()
    cursor.select(QtGui.QTextCursor.SelectionType.LineUnderCursor)
    source.setTextCursor(cursor)
    editor = SimpleNamespace(file_path="example.py", language="python", editor=source,
                             get_code=source.toPlainText)
    team_requests = []
    team = SimpleNamespace(busy=False, submit=lambda text: team_requests.append(text) or True)
    group = SimpleNamespace(widget=lambda _: team)
    owner = SimpleNamespace(get_current_editor=lambda: editor, _find_special_tab=lambda _: (group, 0))
    panel = _panel(owner)
    qtbot.addWidget(panel)
    monkeypatch.setattr(panel, "_access_mode", lambda: "full")
    requests = []

    def converse(prompt, history, context, **kwargs):
        requests.append((history, context, kwargs["tools"]))
        return "Краткое объяснение"

    monkeypatch.setattr(panel, "_service", lambda: SimpleNamespace(
        configuration_problem=lambda: None, is_teaching=False, converse=converse))
    result = panel._quick_result_dialog()
    panel.input.setPlainText("/explain")
    with qtbot.waitSignal(result.answered, timeout=5000):
        panel.send_message()
    qtbot.waitUntil(lambda: result.worker is None)
    history, context, tools = requests[0]
    assert history == [] and tools is None
    assert "selected_code" in context and "other_code" not in context
    assert not panel._history and not team_requests
    assert result.answer.text() == "Краткое объяснение"

    panel.input.setPlainText("Продолжи работу над проектом")
    panel.send_message()
    assert team_requests == ["Продолжи работу над проектом"]
    result.close()
