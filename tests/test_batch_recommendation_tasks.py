"""Batch conversion of analyzer recommendations into project tasks."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.project_tasks import TaskStore


class _FakeTodoPanel:
    def __init__(self, root):
        self.task_store = TaskStore(root)
        self.refreshed = 0

    def refresh_tasks(self):
        self.refreshed += 1


def _tip(title, line, priority="medium"):
    return {
        "category": "Сложность",
        "title": title,
        "explanation": f"Пояснение для {title}",
        "action": "Разбить функцию",
        "file": "app.py",
        "line": line,
        "priority": priority,
    }


def test_batch_recommendations_become_tasks(tmp_path, monkeypatch, qtbot):
    from widgets.ProjectIntelligenceDashboard import ProjectIntelligenceDashboard
    from widgets.Dialog import CustomDialog

    monkeypatch.setattr(CustomDialog, "exec", lambda self: 0)
    panel = ProjectIntelligenceDashboard()
    qtbot.addWidget(panel)
    fake = _FakeTodoPanel(tmp_path)
    panel.todo_panel = fake
    panel.student_tips = [_tip("Длинная функция", 10, "high"), _tip("Глубокая вложенность", 20)]
    panel._filter_tips()

    assert panel.tips_to_tasks_btn.isEnabled()
    assert "2" in panel.tips_to_tasks_btn.text()

    panel._tasks_from_recommendations()
    assert fake.refreshed == 1
    assert fake.task_store.stats()["total"] == 2
    assert [task.priority for task in fake.task_store.tasks] == ["high", "medium"]
    assert all(task.source == "analysis" for task in fake.task_store.tasks)
    assert "Что сделать: Разбить функцию" in fake.task_store.tasks[0].details

    # Repeated conversion must not duplicate the same findings.
    panel._tasks_from_recommendations()
    assert fake.task_store.stats()["total"] == 2
    assert fake.refreshed == 2

    panel.tips_search.setText("вложенность")
    panel._filter_tips()
    assert "1" in panel.tips_to_tasks_btn.text()
    assert panel.tips_to_tasks_btn.isEnabled()
