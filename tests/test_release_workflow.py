import json
import sys

from extensions.manager import ExtensionManager


def test_project_extensions_load_only_after_explicit_confirmation(tmp_path, monkeypatch):
    project = tmp_path / "project"
    extensions = project / ".pyscribe" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "pyscribe-extension.json").write_text(
        json.dumps({"id": "project.extension", "entry": "extension.py"}), encoding="utf-8")
    installed = tmp_path / "app" / "extensions" / "installed"
    installed.mkdir(parents=True)
    (installed / "pyscribe-extension.json").write_text(
        json.dumps({"id": "installed.extension", "entry": "extension.py"}), encoding="utf-8")
    manager = ExtensionManager(tmp_path / "app")
    loaded = []
    monkeypatch.setattr(manager, "_activate", lambda _, spec: loaded.append(spec["id"]))

    manager.set_project_root(project)
    assert loaded == ["installed.extension"]
    loaded.clear()
    manager.set_project_root(project, confirm=lambda _: False)
    assert loaded == ["installed.extension"]
    loaded.clear()
    manager.set_project_root(project, confirm=lambda root: root == project)
    assert loaded == ["project.extension", "installed.extension"]


def test_results_panel_reports_failure_navigates_and_reruns_one(qtbot, tmp_path):
    from widgets.TestResultsPanel import TestResultsPanel

    source = tmp_path / "test_demo.py"
    source.write_text("def test_pass():\n    assert True\n\n"
                      "def test_failure():\n    assert False\n", encoding="utf-8")
    visits = []
    panel = TestResultsPanel(tmp_path, sys.executable, lambda path, line: visits.append((path, line)))
    qtbot.addWidget(panel)
    with qtbot.waitSignal(panel.run_finished, timeout=15_000) as completed:
        panel.start()
    assert completed.args == [1]
    assert [row["outcome"] for row in panel.results] == ["failed", "passed"]
    panel.failures_only.setChecked(True)
    assert not panel.tree.topLevelItem(0).isHidden()
    assert panel.tree.topLevelItem(1).isHidden()
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    panel._open()
    assert visits == [(str(source), 5)]

    with qtbot.waitSignal(panel.run_finished, timeout=15_000) as completed:
        panel.rerun_button.click()
    assert completed.args == [1]
    assert [row["id"] for row in panel.results] == ["test_demo.py::test_failure"]
