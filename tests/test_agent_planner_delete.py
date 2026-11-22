"""Удаление отложенных заданий: хранилище TeamCache и панель AgentPlanner."""
import time
from types import SimpleNamespace

import pytest


def test_planner_collapse_keeps_scheduling_active(panel):
    planner = panel.planner
    assert planner.tree.isHidden()
    assert planner.timer.isActive()
    planner.collapse_button.click()
    assert not planner.tree.isHidden()
    planner.collapse_button.click()
    assert planner.tree.isHidden()
    assert planner.timer.isActive()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from PyQt6 import QtWidgets
    from widgets.AgentPlanner import AgentPlanner

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(AgentPlanner, "cache_dir", staticmethod(lambda: tmp_path / "cache"))
    project = str(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    team = QtWidgets.QWidget()
    team._agent_panel = SimpleNamespace(_project_root=lambda: project)
    team.show_snapshots = team.show_journals = lambda: None
    team.busy, team._question, team._edits = False, "", []
    team.submit = lambda prompt, scheduled_role=None: True
    notes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.No)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda box, title, text, *args, **kwargs: notes.append(text))
    planner = AgentPlanner(team)
    yield SimpleNamespace(planner=planner, project=project, other=str(other), notes=notes, app=app)
    planner.timer.stop()
    team.deleteLater()
    app.processEvents()


def add_job(panel, title="Проверить", role="test", project=None, due=None):
    """Кладёт задание с будущим сроком, чтобы refresh() не запустил его сам."""
    project = project or panel.project
    panel.planner.cache.save_job(project, title, role, "Проверить граф",
                                 int(due if due is not None else time.time() + 3600), "")
    rows = [row for row in panel.planner.cache.jobs(project) if row["title"] == title]
    assert len(rows) == 1
    return dict(rows[0])


def tree_jobs(planner):
    """Все задания из дерева планировщика (группа роли -> дочерние элементы)."""
    from PyQt6 import QtCore

    items = []
    for index in range(planner.tree.topLevelItemCount()):
        group = planner.tree.topLevelItem(index)
        for child in range(group.childCount()):
            items.append(group.child(child).data(0, QtCore.Qt.ItemDataRole.UserRole))
    return items


def press_delete(panel, job, monkeypatch, answer=True):
    """Открывает диалог задания и нажимает «Удалить задание» без реальных окон."""
    from PyQt6 import QtWidgets

    seen = {}

    def fake_exec(dialog):
        seen["button"] = dialog.findChild(QtWidgets.QPushButton, "agentPlannerDelete")
        seen["button"].click()
        return QtWidgets.QDialog.DialogCode.Rejected

    def fake_confirm(box):
        seen["text"] = box.text()
        return (QtWidgets.QMessageBox.StandardButton.Yes if answer
                else QtWidgets.QMessageBox.StandardButton.No)

    monkeypatch.setattr(QtWidgets.QDialog, "exec", fake_exec)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", fake_confirm)
    panel.planner.edit(job)
    return seen


def test_confirmed_delete_removes_job_from_cache_and_tree(panel, monkeypatch):
    job = add_job(panel)
    panel.planner.refresh()
    assert panel.planner.tree.topLevelItemCount() == 1
    assert [item["id"] for item in tree_jobs(panel.planner)] == [job["id"]]

    seen = press_delete(panel, job, monkeypatch, answer=True)

    assert "Проверить" in seen["text"]
    assert panel.planner.cache.jobs(panel.project) == []
    assert panel.planner.tree.topLevelItemCount() == 0
    assert tree_jobs(panel.planner) == []


def test_cancel_keeps_job_in_cache_and_tree(panel, monkeypatch):
    job = add_job(panel)
    panel.planner.refresh()

    press_delete(panel, job, monkeypatch, answer=False)

    assert [row["id"] for row in panel.planner.cache.jobs(panel.project)] == [job["id"]]
    panel.planner.refresh()
    assert [item["id"] for item in tree_jobs(panel.planner)] == [job["id"]]


def test_running_job_delete_is_disabled_and_store_rejects_it(panel, monkeypatch):
    job = add_job(panel)
    assert panel.planner.cache.state(panel.project, job["id"], "running", "queued")
    panel.planner.refresh()
    job = dict(panel.planner.cache.jobs(panel.project)[0])  # свежее состояние, как в дереве
    assert job["state"] == "running"

    seen = press_delete(panel, job, monkeypatch, answer=True)

    assert seen["button"] is not None and not seen["button"].isEnabled()
    rows = panel.planner.cache.jobs(panel.project)
    assert [row["id"] for row in rows] == [job["id"]] and rows[0]["state"] == "running"
    with pytest.raises(ValueError):
        panel.planner.cache.remove_job(panel.project, job["id"])
    assert [item["id"] for item in tree_jobs(panel.planner)] == [job["id"]]


def test_foreign_project_job_is_hidden_and_survives_delete(panel, monkeypatch):
    foreign = add_job(panel, title="Чужое", role="code", project=panel.other)
    panel.planner.refresh()

    assert panel.planner.tree.topLevelItemCount() == 0
    assert tree_jobs(panel.planner) == []

    press_delete(panel, foreign, monkeypatch, answer=True)

    assert [row["id"] for row in panel.planner.cache.jobs(panel.other)] == [foreign["id"]]
    assert panel.planner.cache.jobs(panel.project) == []
    assert any("уже удалено или недоступно" in note for note in panel.notes)
    assert panel.planner.tree.topLevelItemCount() == 0
