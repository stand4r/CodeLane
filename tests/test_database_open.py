"""Tests for opening databases from the explorer and the SQL workbench.

Run: py -3.14 -m pytest tests/test_database_open.py
"""
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from PyQt6 import QtGui, QtWidgets

from core.database_workspace import (
    DatabaseProfile,
    connection_file_id,
    read_connection_file,
    write_connection_file,
)
from widgets.DatabaseWorkspace import DatabaseManager, DatabaseWorkbench

_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def _pump(predicate, timeout=10.0):
    """Spin the GUI event loop until predicate() is true."""
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


class _WorkbenchWindow(QtWidgets.QWidget):
    """Minimal stand-in for ModernMainWindow with real tab groups."""

    def __init__(self):
        super().__init__()
        self.tabs = QtWidgets.QTabWidget()
        QtWidgets.QVBoxLayout(self).addWidget(self.tabs)
        self.active_tab_widget = self.tabs
        self.app_manager = SimpleNamespace(session_files=[], recent_files=[], logger=None)

    def editor_groups(self):
        return [self.tabs]

    def _set_active_group(self, group):
        self.active_tab_widget = group


@pytest.fixture
def window(monkeypatch, tmp_path):
    _app()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    win = _WorkbenchWindow()
    win.database_manager = DatabaseManager(win)
    yield win
    win.deleteLater()


def _connection_file(path):
    profile = DatabaseProfile(
        id=connection_file_id(path), name="Diplom", kind="postgresql",
        database="diplom", host="localhost", port=5432, user="postgres",
        password_env="DIPLOM_DB_PASSWORD",
    )
    write_connection_file(path, profile)
    return profile


def _sqlite_file(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "INSERT INTO students (name) VALUES ('Анна');"
    )
    connection.commit()
    connection.close()


def test_connection_file_roundtrip(tmp_path):
    path = tmp_path / "diplom.dbconn"
    profile = _connection_file(path)

    loaded = read_connection_file(path)
    assert loaded.id == connection_file_id(path) == profile.id
    assert (loaded.kind, loaded.database, loaded.host, loaded.port) == ("postgresql", "diplom", "localhost", 5432)
    assert loaded.user == "postgres"
    assert loaded.password_env == "DIPLOM_DB_PASSWORD"


def test_connection_file_rejects_broken_payload(tmp_path):
    path = tmp_path / "broken.dbconn"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        read_connection_file(path)

    path.write_text('{"version": 2, "kind": "postgresql", "database": "db", "host": "localhost"}',
                    encoding="utf-8")
    with pytest.raises(ValueError):
        read_connection_file(path)

    # A SQLite profile is not a server connection: the file must be rejected.
    path.write_text('{"version": 1, "kind": "sqlite", "database": "x.db"}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_connection_file(path)


def test_open_connection_file_shows_tab_and_reuses_it(window, tmp_path):
    path = tmp_path / "diplom.dbconn"
    _connection_file(path)

    widget = window.database_manager.open_connection_path(str(path))
    assert window.tabs.count() == 1
    assert window.tabs.currentWidget() is widget
    assert isinstance(widget, DatabaseWorkbench)
    assert widget.property("pyscribeSpecial") == "database"
    assert widget.database_id == connection_file_id(path)
    assert widget.session is None
    assert "localhost" in widget.connection.text()
    assert widget.status.text() == "Подключение закрыто"
    assert window.tabs.tabText(0) == "diplom.dbconn"

    # Opening the same connection again must focus the existing tab, not add one.
    again = window.database_manager.open_connection_path(str(path))
    assert again is widget
    assert window.tabs.count() == 1


def test_open_sqlite_creates_tab_and_reports_result(window, tmp_path):
    db = tmp_path / "diplom.db"
    _sqlite_file(db)

    widget = window.database_manager.open_sqlite_path(str(db))
    assert isinstance(widget, DatabaseWorkbench)
    assert window.tabs.count() == 1
    assert window.tabs.currentWidget() is widget

    assert _pump(lambda: not widget.working)
    if widget.session is None:
        # Without the SQLAlchemy dependency the tab still opens and explains why.
        assert widget.status.text()
    else:
        assert widget.status.text() == ""
        assert _pump(lambda: widget.objects.count() == 1)
        assert widget.objects.item(0).text() == "students"


def test_open_sqlite_lists_tables_and_data(window, tmp_path):
    pytest.importorskip("sqlalchemy")
    db = tmp_path / "diplom.db"
    _sqlite_file(db)

    widget = window.database_manager.open_sqlite_path(str(db))
    assert _pump(lambda: widget.session is not None and widget.objects.count() == 1)
    assert [widget.objects.item(i).text() for i in range(widget.objects.count())] == ["students"]

    # Selecting a table opens the "Данные" page and loads the first page of rows.
    widget.objects.setCurrentItem(widget.objects.item(0))
    assert widget.pages.currentIndex() == 1
    assert _pump(lambda: widget.record_model.rowCount() == 1)
    assert widget.record_model.columnCount() == 2
    assert widget.page_label.text() == "1–1"


def test_missing_database_file_keeps_tab_and_shows_error(window, tmp_path):
    missing = tmp_path / "absent.db"

    widget = window.database_manager.open_sqlite_path(str(missing))
    assert isinstance(widget, DatabaseWorkbench)
    assert _pump(lambda: not widget.working)
    assert widget.session is None
    assert widget.status.text()


def test_open_sql_stages_query_into_bound_sqlite_workbench(window, tmp_path):
    db = tmp_path / "diplom.db"
    _sqlite_file(db)
    window.database_manager.set_project(tmp_path)
    window.database_manager.bind(DatabaseProfile.sqlite(db))

    source = tmp_path / "report.sql"
    source.write_text("SELECT * FROM students;\n", encoding="utf-8")
    assert window.database_manager.open_sql(source.read_text(encoding="utf-8"), str(source)) is True

    widget = window.tabs.currentWidget()
    assert isinstance(widget, DatabaseWorkbench)
    assert widget.sql.toPlainText() == "SELECT * FROM students;\n"
    assert widget.pages.currentIndex() == 0


def test_sql_draft_keeps_edits_and_detects_external_changes(window, tmp_path, monkeypatch):
    draft = DatabaseWorkbench(window.database_manager, "draft")
    source_path = tmp_path / "source.sql"
    destination = tmp_path / "draft.sql"
    copy = tmp_path / "draft-copy.sql"
    paths = iter((destination, copy))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *args: (str(next(paths)), ""))

    assert draft.load_sql("SELECT 1;\r\n", source_path)
    draft.sql.insertPlainText("-- comment\n")
    assert draft.load_sql(draft.sql.toPlainText(), source_path)
    assert draft.sql.document().isModified()
    assert draft.sql.document().isUndoAvailable()

    save = next(action for action in draft.findChildren(QtGui.QAction) if action.text() == "Сохранить SQL")
    save.trigger()
    assert destination.read_bytes() == draft.sql.toPlainText().encode("utf-8")
    draft.sql.selectAll()
    draft.sql.insertPlainText("")
    assert draft.sql.document().isModified()
    assert draft.sql.toPlainText() == ""
    buttons = QtWidgets.QMessageBox.StandardButton
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args: buttons.Cancel)
    assert not draft.confirm_close()
    assert draft.save_sql()
    assert destination.read_bytes() == b""

    destination.write_bytes(b"SELECT 'external';")
    draft.sql.insertPlainText("SELECT 'local';")
    assert not draft.save_sql()
    assert draft.sql.document().isModified()
    assert destination.read_bytes() == b"SELECT 'external';"
    assert draft.save_sql(choose_path=True)
    assert copy.read_bytes() == b"SELECT 'local';"


def test_file_manager_routes_dbconn_to_database_workbench(window, tmp_path):
    from app.file_manager import FileManager

    path = tmp_path / "diplom.dbconn"
    _connection_file(path)

    window._find_and_focus_open_tab = lambda _path: False
    FileManager(window).open_file(str(path))

    assert window.tabs.count() == 1
    assert isinstance(window.tabs.currentWidget(), DatabaseWorkbench)
    assert window.tabs.currentWidget().database_id == connection_file_id(path)


def test_file_manager_rejects_missing_file(window, tmp_path):
    """A missing .dbconn must not crash the open path (dialog is modal, so patch it)."""
    from app import file_manager as module

    shown = []
    monkey = module.CustomDialog
    module.CustomDialog = lambda message: SimpleNamespace(exec=lambda: shown.append(message))
    try:
        window._find_and_focus_open_tab = lambda _path: False
        module.FileManager(window).open_file(str(tmp_path / "nope.dbconn"))
    finally:
        module.CustomDialog = monkey
    assert shown and "не найден" in shown[0].lower()
    assert window.tabs.count() == 0
