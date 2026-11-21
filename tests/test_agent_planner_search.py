"""Поиск по отложенным заданиям в AgentPlanner: фильтр видимости, пустые подписи, запуск скрытых.

Фикстура ``panel`` и помощники ``add_job`` / ``tree_jobs`` / ``press_delete`` скопированы
из ``tests/test_agent_planner_delete.py``, чтобы файл запускался автономно.
"""
import time
from types import SimpleNamespace

import pytest


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
    submits = []
    team.submit = lambda prompt, scheduled_role=None: (submits.append((prompt, scheduled_role)), True)[1]
    notes = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *args, **kwargs: QtWidgets.QMessageBox.StandardButton.No)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda box, title, text, *args, **kwargs: notes.append(text))
    planner = AgentPlanner(team)
    app.processEvents()  # погасить отложенный QTimer.singleShot(0, refresh) вне теста
    yield SimpleNamespace(planner=planner, project=project, other=str(other), notes=notes,
                          app=app, submits=submits)
    planner.timer.stop()
    team.deleteLater()
    app.processEvents()


def add_job(panel, title="Проверить", role="test", project=None, due=None, prompt="Текст задачи"):
    """Кладёт задание с будущим сроком, чтобы refresh() не запустил его сам."""
    project = project or panel.project
    panel.planner.cache.save_job(project, title, role, prompt,
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


def tree_titles(planner):
    return [job["title"] for job in tree_jobs(planner)]


def label_visible(planner):
    """Видимость подписи без показа окна: явное скрытие и скрытие дерева учитываются."""
    return not planner.empty_label.isHidden() and not planner.tree.isHidden()


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


def open_dialog(panel, job, monkeypatch):
    """Открывает диалог задания и снимает предзаполненные поля, не показывая окно."""
    from PyQt6 import QtWidgets

    seen = {}

    def fake_exec(dialog):
        seen["title"] = dialog.windowTitle()
        seen["lines"] = [widget.text() for widget in dialog.findChildren(QtWidgets.QLineEdit)]
        seen["plain"] = [widget.toPlainText() for widget in dialog.findChildren(QtWidgets.QPlainTextEdit)]
        seen["delete_button"] = dialog.findChild(QtWidgets.QPushButton, "agentPlannerDelete")
        return QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(QtWidgets.QDialog, "exec", fake_exec)
    panel.planner.edit(job)
    return seen


def test_search_by_title_is_case_insensitive(panel):
    target = add_job(panel, title="Проверить кэш", prompt="Синтетика один")
    add_job(panel, title="Собрать отчёт", prompt="Синтетика два")

    panel.planner.search.setText("ПРОВЕР")

    assert [job["id"] for job in tree_jobs(panel.planner)] == [target["id"]]
    assert tree_titles(panel.planner) == ["Проверить кэш"]
    assert not label_visible(panel.planner)


def test_search_matches_prompt_of_job(panel):
    target = add_job(panel, title="Разбор", prompt="Изучить хранилище команды")
    add_job(panel, title="Собрать отчёт", prompt="Синтетика два")

    panel.planner.search.setText("ХРАНИЛИЩЕ")

    assert tree_titles(panel.planner) == ["Разбор"]
    assert [job["id"] for job in tree_jobs(panel.planner)] == [target["id"]]


def test_filter_updates_on_each_keystroke(panel):
    add_job(panel, title="Проверить граф", prompt="Синтетика один")
    add_job(panel, title="Собрать отчёт", prompt="Синтетика два")
    add_job(panel, title="Проверить кэш", prompt="Синтетика три")
    panel.planner.refresh()
    assert len(tree_titles(panel.planner)) == 3

    field = panel.planner.search
    steps = []
    for char in "Проверить к":
        field.setText(field.text() + char)
        steps.append(set(tree_titles(panel.planner)))

    assert len(steps) == len("Проверить к")
    assert steps[4] == {"Проверить граф", "Проверить кэш"}  # «Провер»
    assert steps[-1] == {"Проверить кэш"}
    assert all(later <= earlier for earlier, later in zip(steps, steps[1:])), steps


def test_clear_search_restores_all_project_jobs(panel):
    first = add_job(panel, title="Проверить граф", prompt="Синтетика один")
    second = add_job(panel, title="Собрать отчёт", prompt="Синтетика два")
    third = add_job(panel, title="Проверить кэш", prompt="Синтетика три")

    panel.planner.search.setText("Проверить кэш")
    assert tree_titles(panel.planner) == ["Проверить кэш"]

    panel.planner.search.setText("")

    assert sorted(job["id"] for job in tree_jobs(panel.planner)) == sorted(
        [first["id"], second["id"], third["id"]])
    assert len(tree_jobs(panel.planner)) == 3
    assert not label_visible(panel.planner)


def test_no_matches_empties_tree_and_shows_not_found_label(panel):
    add_job(panel, title="Проверить граф")
    add_job(panel, title="Собрать отчёт")

    panel.planner.search.setText("нет такого задания")

    assert panel.planner.tree.topLevelItemCount() == 0
    assert tree_jobs(panel.planner) == []
    assert panel.planner.empty_label.text() == "Ничего не найдено"
    assert label_visible(panel.planner)


def test_no_jobs_at_all_shows_scheduled_empty_label(panel):
    panel.planner.refresh()

    assert panel.planner.cache.jobs(panel.project) == []
    assert panel.planner.tree.topLevelItemCount() == 0
    assert panel.planner.empty_label.text() == "Нет запланированных задач"
    assert label_visible(panel.planner)

    add_job(panel, title="Проверить кэш")
    panel.planner.refresh()
    assert tree_titles(panel.planner) == ["Проверить кэш"]
    assert not label_visible(panel.planner)


def test_hidden_by_filter_job_still_runs(panel):
    panel.planner.search.setText("совпадение")
    panel.planner.refresh()
    job = add_job(panel, title="Проверить граф", prompt="Совсем другой текст",
                  due=time.time() - 60)
    assert panel.submits == []

    panel.planner.refresh()

    assert tree_titles(panel.planner) == []  # отфильтровано
    assert panel.planner.empty_label.text() == "Ничего не найдено"
    assert len(panel.submits) == 1
    assert "Проверить граф" in panel.submits[0][0]
    assert panel.submits[0][1] == "test"
    assert panel.planner.active == (panel.project, job["id"])
    rows = panel.planner.cache.jobs(panel.project)
    assert [row["id"] for row in rows] == [job["id"]]
    assert rows[0]["state"] == "running"
    assert rows[0]["owner"] == panel.planner.cache.owner


def test_foreign_project_jobs_never_match_filter(panel):
    local = add_job(panel, title="Проверить кэш", prompt="Синтетика один")
    foreign = add_job(panel, title="Проверить чужое", role="code", project=panel.other,
                      prompt="Проверить кэш чужого проекта")

    panel.planner.search.setText("Проверить")
    panel.planner.refresh()

    assert [job["id"] for job in tree_jobs(panel.planner)] == [local["id"]]
    assert foreign["id"] not in [job["id"] for job in tree_jobs(panel.planner)]
    assert [row["id"] for row in panel.planner.cache.jobs(panel.other)] == [foreign["id"]]
    assert [row["id"] for row in panel.planner.cache.jobs(panel.project)] == [local["id"]]


def test_filtered_job_opens_edit_dialog_with_full_data(panel, monkeypatch):
    job = add_job(panel, title="Проверить кэш", role="test", prompt="Синтетика один")
    add_job(panel, title="Собрать отчёт", prompt="Синтетика два")

    panel.planner.search.setText("Проверить кэш")
    visible = tree_jobs(panel.planner)
    assert len(visible) == 1
    item = visible[0]
    assert {"id", "title", "prompt", "role", "state", "due"} <= set(item)
    assert item["id"] == job["id"] and item["prompt"] == job["prompt"]

    seen = open_dialog(panel, item, monkeypatch)

    assert seen["title"] == "Отложенное задание"
    assert job["title"] in seen["lines"]
    assert job["prompt"] in seen["plain"]
    assert seen["delete_button"] is not None and seen["delete_button"].isEnabled()


def test_filtered_job_can_be_deleted_from_tree(panel, monkeypatch):
    job = add_job(panel, title="Проверить кэш", prompt="Синтетика один")
    remaining = add_job(panel, title="Собрать отчёт", prompt="Синтетика два")

    panel.planner.search.setText("Проверить кэш")
    visible = tree_jobs(panel.planner)
    assert [item["id"] for item in visible] == [job["id"]]

    seen = press_delete(panel, visible[0], monkeypatch, answer=True)

    assert "Проверить кэш" in seen["text"]
    assert [row["id"] for row in panel.planner.cache.jobs(panel.project)] == [remaining["id"]]
    # Фильтр всё ещё активен: удалённое задание не вернулось, подпись — «Ничего не найдено».
    assert tree_jobs(panel.planner) == []
    assert panel.planner.empty_label.text() == "Ничего не найдено"

    panel.planner.search.setText("")
    assert tree_titles(panel.planner) == ["Собрать отчёт"]


def test_collapse_with_active_filter_hides_tree_but_keeps_timer(panel):
    add_job(panel, title="Проверить кэш", prompt="Синтетика один")
    add_job(panel, title="Собрать отчёт", prompt="Синтетика два")
    panel.planner.search.setText("Проверить")
    assert len(tree_jobs(panel.planner)) == 1

    panel.planner._toggle_collapsed()
    assert panel.planner.tree.isHidden()
    assert panel.planner.search.isHidden()
    assert panel.planner.timer.isActive()
    assert not label_visible(panel.planner)

    panel.planner._toggle_collapsed()
    assert not panel.planner.tree.isHidden()
    assert not panel.planner.search.isHidden()
    assert panel.planner.timer.isActive()
    assert panel.planner.search.text() == "Проверить"
    assert tree_titles(panel.planner) == ["Проверить кэш"]
