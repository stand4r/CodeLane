"""Agent navigation uses live CodeLane actions and respects access settings."""
import json
from types import SimpleNamespace

import pytest

from app.agent_workbench import run_workbench
from core import desktop_control
from services.agent_tools import LocalToolbox, ToolError


class _ActivityBar:
    active_sidebar_key = "explorer"

    def set_activity(self, name):
        self.active_sidebar_key = name


class _Window:
    def __init__(self):
        self.activity_bar = _ActivityBar()
        self.sidebar_visible = True
        self.terminal_visible = False
        self.saved = False

    def toggle_terminal(self):
        self.terminal_visible = not self.terminal_visible

    def action_save_all_files(self):
        self.saved = True

    def get_current_editor(self):
        return SimpleNamespace(file_path="main.py")

    def action_run_file(self):
        self.ran = True


def test_workbench_control_navigates_and_runs_actions():
    window = _Window()
    assert run_workbench(window, "workbench_control", {"action": "show_search"}, None)["active_panel"] == "search"
    assert run_workbench(window, "workbench_control", {"action": "show_terminal"}, None)["terminal_visible"]
    assert run_workbench(window, "workbench_control", {"action": "show_terminal"}, None)["terminal_visible"]
    assert not run_workbench(window, "workbench_control", {"action": "hide_terminal"}, None)["terminal_visible"]
    run_workbench(window, "workbench_control", {"action": "save_all"}, None)
    run_workbench(window, "workbench_control", {"action": "run_file"}, None)
    assert window.saved and window.ran


def test_workbench_control_is_gated_and_validated(tmp_path):
    calls = []
    box = LocalToolbox(str(tmp_path), ui_action=lambda name, payload: calls.append((name, payload)) or "ok")
    assert box.run("workbench_control", json.dumps({"action": "show_settings"})) == "ok"
    assert calls == [("workbench_control", {"action": "show_settings"})]
    with pytest.raises(ToolError, match="Недопустимое значение"):
        box.run("workbench_control", json.dumps({"action": "delete_everything"}))
    with pytest.raises(ToolError, match="Сохранение файлов отключено"):
        LocalToolbox(str(tmp_path), allow_write=False, ui_action=lambda *_: "ok").run(
            "workbench_control", json.dumps({"action": "save_all"}))
    with pytest.raises(ToolError, match="запрещён"):
        LocalToolbox(str(tmp_path), access_mode="read_only", ui_action=lambda *_: "ok").run(
            "workbench_control", json.dumps({"action": "show_settings"}))


def test_desktop_state_lists_windows_without_input(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "screen_rect", lambda: {"x": 0, "y": 0, "width": 100, "height": 80})
    monkeypatch.setattr(desktop_control, "cursor_position", lambda: (12, 18))
    monkeypatch.setattr(desktop_control, "foreground_window", lambda: {"title": "CodeLane"})
    monkeypatch.setattr(desktop_control, "list_windows", lambda limit: [{"title": "CodeLane"}])
    state = json.loads(LocalToolbox(str(tmp_path)).run("desktop_state", "{}"))
    assert state["foreground"]["title"] == "CodeLane"
    assert state["windows"] == [{"title": "CodeLane"}]
    with pytest.raises(ToolError, match="запрещён"):
        LocalToolbox(str(tmp_path), access_mode="read_only").run("desktop_state", "{}")
