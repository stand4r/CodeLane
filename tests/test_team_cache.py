import time
import pytest

from services.agent_edits import FileEdit, EditCandidate
from services.agent_tools import LocalToolbox, ToolError
from services.team_cache import TeamCache


def test_snapshot_restore_guard_and_expiry(tmp_path):
    db = TeamCache(tmp_path / "cache")
    project = str(tmp_path)
    box = LocalToolbox(project)
    path = tmp_path / "code.txt"
    path.write_text("original")
    db.apply(project, FileEdit("code.txt", "original", ""), box)
    snapshot = db.snapshots(project)[0]["id"]
    path.write_text("later")
    with pytest.raises(ToolError):
        db.restore(project, snapshot, box)
    assert path.read_text() == "later"
    assert db.contents(project, snapshot)[0].before == "original"
    path.write_text("")
    db.restore(project, snapshot, box)
    assert path.read_text() == "original"
    with db.connect() as conn:
        conn.execute("UPDATE snapshots SET created=?", (time.time() - 15 * 86400,))
    assert not db.snapshots(project)


def test_plan_claim_is_project_scoped_and_one_shot(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project = str(tmp_path)
    cache.save_job(project, "Проверка", "test", "Проверить граф", 0, "code.txt")
    job = cache.jobs(project)[0]
    assert not cache.jobs("other")
    assert cache.state(project, job["id"], "running", "queued")
    assert not cache.state(project, job["id"], "running", "queued")
    with pytest.raises(ValueError):
        cache.save_job(project, "Test", "test", "test", 0, "../other.txt")


def test_remove_job_deletes_only_own_project(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project, other = str(tmp_path), str(tmp_path / "other")
    (tmp_path / "other").mkdir()
    cache.save_job(project, "Своё", "code", "Сделать", 0, "code.txt")
    cache.save_job(other, "Чужое", "code", "Сделать", 0, "code.txt")
    job = cache.jobs(project)[0]["id"]
    foreign = cache.jobs(other)[0]["id"]
    assert cache.remove_job(project, job) is True
    assert not cache.jobs(project)
    assert cache.remove_job(project, job) is False
    assert cache.remove_job(project, foreign) is False
    assert [row["id"] for row in cache.jobs(other)] == [foreign]


def test_remove_job_rejects_running_and_allows_paused(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project = str(tmp_path)
    cache.save_job(project, "Задача", "code", "Сделать", 0, "")
    job = cache.jobs(project)[0]["id"]
    assert cache.state(project, job, "running", "queued")
    with pytest.raises(ValueError) as error:
        cache.remove_job(project, job)
    assert str(error.value) == "Задание выполняется и его нельзя удалить."
    assert [row["state"] for row in cache.jobs(project)] == ["running"]
    assert cache.state(project, job, "paused", "running")
    assert cache.remove_job(project, job) is True
    assert not cache.jobs(project)


def test_remove_job_deletes_finished_job(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project = str(tmp_path)
    cache.save_job(project, "Завершённая", "code", "Сделать", 0, "")
    job = cache.jobs(project)[0]["id"]
    assert cache.state(project, job, "running", "queued")
    assert cache.state(project, job, "done", "running")
    assert cache.jobs(project)[0]["state"] == "done"
    assert cache.remove_job(project, job) is True
    assert cache.jobs(project) == []


def test_remove_job_foreign_running_job_returns_false(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project, other = str(tmp_path), str(tmp_path / "other")
    (tmp_path / "other").mkdir()
    cache.save_job(other, "Чужое выполняется", "code", "Сделать", 0, "")
    foreign = cache.jobs(other)[0]["id"]
    assert cache.state(other, foreign, "running", "queued")
    assert cache.remove_job(project, foreign) is False  # не ValueError: задание чужого проекта
    rows = cache.jobs(other)
    assert [row["id"] for row in rows] == [foreign]
    assert rows[0]["state"] == "running"
    assert cache.jobs(project) == []


def test_remove_job_deletes_row_with_empty_state(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    project = str(tmp_path)
    cache.save_job(project, "Без статуса", "code", "Сделать", 0, "")
    job = cache.jobs(project)[0]["id"]
    with cache.connect() as conn:
        conn.execute("UPDATE jobs SET state=NULL WHERE id=?", (job,))
    assert cache.remove_job(project, job) is True
    assert cache.jobs(project) == []


def test_restore_run_validates_all_files_before_changing_any(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    box = LocalToolbox(str(tmp_path))
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("old")
        cache.apply(str(tmp_path), FileEdit(name, "old", "new"), box, "run-1")
    (tmp_path / "a.txt").write_text("manual")
    with pytest.raises(ValueError, match="a.txt"):
        cache.restore_run(str(tmp_path), "run-1", box)
    assert (tmp_path / "b.txt").read_text() == "new"
    (tmp_path / "a.txt").write_text("new")
    cache.restore_run(str(tmp_path), "run-1", box)
    assert all((tmp_path / n).read_text() == "old" for n in ("a.txt", "b.txt"))


def test_restore_run_combines_successive_edits_to_the_same_file(tmp_path):
    cache = TeamCache(tmp_path / "cache")
    box = LocalToolbox(str(tmp_path))
    (tmp_path / "demo.py").write_text("first")
    cache.apply(str(tmp_path), FileEdit("demo.py", "first", "second"), box, "run")
    cache.apply(str(tmp_path), FileEdit("demo.py", "second", "third"), box, "run")
    cache.restore_run(str(tmp_path), "run", box)
    assert (tmp_path / "demo.py").read_text() == "first"


def test_reconcile_requires_full_coverage(tmp_path):
    (tmp_path / "demo.txt").write_text("abc def")
    candidate = EditCandidate(LocalToolbox(str(tmp_path)))
    candidate.add(dict(path="demo.txt", old_text="abc", new_text="ABC"), agents={2})
    with pytest.raises(ValueError):
        candidate.add(dict(path="demo.txt", old_text="bc", new_text="XX"), reconcile=True)
    candidate.add(dict(path="demo.txt", old_text="abc def", new_text="ABC DEF"), reconcile=True, agents={3})
    assert candidate.files()[0].after == "ABC DEF"
    assert candidate.files()[0].agents == (2, 3)


def test_planner_waits_for_both_conditions_and_runs_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PyQt6 import QtWidgets
    from widgets.AgentPlanner import AgentPlanner

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(AgentPlanner, "cache_dir", staticmethod(lambda: tmp_path / "cache"))
    project = str(tmp_path)
    team = QtWidgets.QWidget()
    team._agent_panel = SimpleNamespace(_project_root=lambda: project)
    team.show_snapshots = team.show_journals = lambda: None
    team.busy, team._question, team._edits = False, "", []
    calls = []
    team.submit = lambda prompt, scheduled_role: calls.append((prompt, scheduled_role)) or True
    planner = AgentPlanner(team)
    planner.cache.save_job(project, "Проверить", "test", "Проверить результат", 0, "ready.txt")
    planner.refresh()
    assert not calls
    (tmp_path / "ready.txt").touch()
    planner.refresh()
    planner.finished(True)
    planner.refresh()
    assert len(calls) == 1 and calls[0][1] == "test"
    assert planner.cache.jobs(project)[0]["state"] == "done"
    team.deleteLater()
    app.processEvents()
