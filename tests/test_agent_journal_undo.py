"""Agent journal: recorded edits, diff preview and undo requests."""
import pytest

pytest.importorskip("PyQt6")

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from widgets.AgentJournalWidget import AgentJournalWidget  # noqa: E402

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

EDITS = [
    {"id": 7, "path": "core/a.py", "state": "pending",
     "diff": "--- a/core/a.py\n+++ b/core/a.py\n@@ -1 +1 @@\n-old\n+new\n"},
    {"id": 9, "path": "core/b.py", "state": "restored",
     "diff": "--- a/core/b.py\n+++ b/core/b.py\n@@ -2 +2 @@\n-x\n+y\n"},
]


def _journal(edits=EDITS):
    return AgentJournalWidget("ИИ-агент", "12:00:00  старт", edits=edits)


def test_edits_are_listed_with_diff_details():
    view = _journal()
    assert view.edits_panel.isVisibleTo(view)
    assert view.edit_list.count() == 2
    assert "core/a.py" in view.edit_list.item(0).text()
    assert view.edits_label.text() == "Правки агента · 2"
    assert "не подтверждено" in view.edit_list.item(0).text()
    assert "отменено" in view.edit_list.item(1).text()
    view.edit_list.setCurrentRow(0)
    details = view.details.toPlainText()
    assert "+new" in details and "-old" in details
    assert "применение не подтверждено" in details
    view.deleteLater()


def test_undo_button_requests_current_edit_only():
    view = _journal()
    requested = []
    view.undoEditRequested.connect(requested.append)
    view.edit_list.setCurrentRow(1)
    assert view.current_edit_id() == 9
    view.undo_button.click()
    assert requested == [9]
    view.deleteLater()


def test_rollback_button_asks_for_whole_run():
    view = _journal()
    asked = []
    view.rollbackRunRequested.connect(lambda: asked.append(True))
    view.rollback_button.click()
    assert asked == [True]
    view.deleteLater()


def test_without_edits_panel_is_hidden():
    view = AgentJournalWidget("ИИ-агент", "12:00:01  нет правок")
    assert not view.edits_panel.isVisibleTo(view)
    view.set_edits(EDITS)
    assert view.edits_panel.isVisibleTo(view)
    view.set_edits([])
    assert not view.edits_panel.isVisibleTo(view)
    view.deleteLater()
