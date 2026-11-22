"""Tests for ProjectManager deletion and toolchain detection."""
import os
import sys
import tempfile
from pathlib import Path

# Ensure root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from widgets.ProjectManager import project_manager
from utils.toolchain_detector import detect_all_toolchains, build_run_command_for_file


def test_project_dialog_navigates_filtered_projects(tmp_path, monkeypatch):
    from PyQt6 import QtCore, QtTest, QtWidgets
    from widgets.ProjectDialog import ProjectDialog
    from widgets.ProjectManager import ProjectConfig

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    projects = [ProjectConfig(name, str(tmp_path / name)) for name in ("Alpha", "Beta", "Alpine")]
    monkeypatch.setattr(project_manager, "get_sorted_projects", lambda: projects)
    opened = []
    monkeypatch.setattr(project_manager, "open_project", lambda path: opened.append(path) or projects[2])
    dialog = ProjectDialog()
    try:
        dialog.show()
        app.processEvents()
        dialog.search_edit.setText("missing")
        assert dialog.empty_projects.isVisible()
        QtTest.QTest.keyClick(dialog.search_edit, QtCore.Qt.Key.Key_Return)
        assert not opened
        assert dialog.isVisible()

        dialog.search_edit.setText("alp")
        assert not dialog.empty_projects.isVisible()
        QtTest.QTest.keyClick(dialog.search_edit, QtCore.Qt.Key.Key_Down)
        assert dialog.selected_project is projects[0]
        QtTest.QTest.keyClick(dialog.search_edit, QtCore.Qt.Key.Key_Down)
        assert dialog.selected_project is projects[2]
        QtTest.QTest.keyClick(dialog.search_edit, QtCore.Qt.Key.Key_Return)
        assert opened == [projects[2].root_path]
        assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_project_config_write_error_propagates(tmp_path):
    import pytest
    from core.project_state import sync_project_config
    from widgets.ProjectManager import ProjectConfig

    (tmp_path / ".codelane").write_text("blocked", encoding="utf-8")
    config = ProjectConfig("Test", str(tmp_path))
    with pytest.raises(OSError):
        sync_project_config(config, str(tmp_path), save=True)


def test_analyzers_workspace_applies_and_persists_an_empty_profile(tmp_path, monkeypatch, qtbot):
    from types import SimpleNamespace

    from PyQt6 import QtWidgets

    from app.project_handler import ProjectHandler
    from core.project_state import sync_project_config
    from widgets.AnalysisProfilesDialog import AnalyzerProfilesEditor
    from widgets.ProjectManager import ProjectConfig

    project = ProjectConfig("Workspace", str(tmp_path), analyzers=["structure"])
    monkeypatch.setattr(project_manager, "projects", {})
    monkeypatch.setattr(project_manager, "current_project", project)
    monkeypatch.setattr(project_manager, "projects_file", tmp_path / "projects.json")
    applied = []
    panel = SimpleNamespace(set_project=lambda root, selected: applied.append((root, selected)))
    handler = ProjectHandler(SimpleNamespace(intelligence_panel=panel))
    editor = AnalyzerProfilesEditor(project, current=project.analyzers)
    qtbot.addWidget(editor)
    editor.apply_requested.connect(lambda selected: handler.apply_analyzers(project, selected))
    editor.profile_picker.setCurrentIndex(editor.profile_picker.findData("none"))
    assert project.analyzers == ["structure"]
    editor.findChild(type(editor.view_button), "primary").click()
    assert project.analyzers == [] and applied == [(str(tmp_path), [])]
    reloaded = ProjectConfig("Workspace", str(tmp_path), analyzers=["structure"])
    sync_project_config(reloaded, str(tmp_path))
    assert reloaded.analyzers == []

    editor._checks["metrics"].setChecked(True)
    assert editor.profile_picker.currentData() == "custom"
    assert editor.selected_analyzers == {"metrics"}
    assert project.analyzers == []

    panel = editor.findChild(QtWidgets.QFrame, "analysisChecksPanel")
    editor.resize(950, 640)
    editor.show()
    qtbot.waitUntil(lambda: editor._wide)
    editor.resize(440, 640)
    qtbot.waitUntil(lambda: editor._wide is False and panel.width() <= editor.width())


def test_project_delete_case_insensitive():
    """Verify projects can be deleted regardless of path casing on Windows."""
    test_dir = os.path.join(tempfile.gettempdir(), "py_scribe_del_test_case")
    os.makedirs(test_dir, exist_ok=True)
    
    cfg = project_manager.create_project(test_dir, name="CaseDelTest")
    assert cfg is not None
    
    # Try deleting with upper case
    res = project_manager.delete_project(test_dir.upper())
    assert res is True
    
    # Verify no longer present
    recent_names = [p.name for p in project_manager.get_recent_projects()]
    assert "CaseDelTest" not in recent_names


def test_toolchain_detector():
    """Verify toolchain detector finds Python and builds valid execution commands."""
    toolchains = detect_all_toolchains()
    assert "python" in toolchains
    assert toolchains["python"]["found"] is True
    
    cmd, err = build_run_command_for_file("script.py")
    assert cmd is not None
    assert "script.py" in cmd
    assert err is None


if __name__ == "__main__":
    test_project_delete_case_insensitive()
    print("test_project_delete_case_insensitive: PASS")
    test_toolchain_detector()
    print("test_toolchain_detector: PASS")
    print("ALL TESTS PASSED!")
