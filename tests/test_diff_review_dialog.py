"""Diff review dialog: hunk selection and partial staging wiring."""
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from widgets.DiffReviewDialog import DiffReviewDialog  # noqa: E402

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

DIFF = """diff --git a/demo.txt b/demo.txt
index 1111111..2222222 100644
--- a/demo.txt
+++ b/demo.txt
@@ -1,3 +1,4 @@
 line1
-line2
+line2 changed
+line2.5
 line3
@@ -10,3 +11,3 @@ def section():
 ctx
-old tail
+new tail
 ctx
"""


class FakeGit:
    """Serves a fixed diff and records the patches handed to ``git apply``."""

    def __init__(self, diff=DIFF, untracked=""):
        self.diff = diff
        self.untracked = untracked
        self.calls = []
        self.patches = []

    def __call__(self, args, input_text=None):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["diff", "--name-only"]:
            return True, "demo.txt\n" if self.diff else ""
        if args[:2] == ["ls-files", "--others"]:
            return True, self.untracked
        if args[:1] == ["diff"] and args[-1] == "demo.txt":
            return True, self.diff
        if args[:1] == ["apply"]:
            self.patches.append(Path(args[-1]).read_text(encoding="utf-8"))
            return True, ""
        if args[:1] == ["add"]:
            return True, ""
        return True, ""


def test_dialog_lists_hunks_and_stages_only_ticked_ones(tmp_path):
    git = FakeGit()
    dialog = DiffReviewDialog(root=str(tmp_path), run_git=git)
    try:
        assert dialog.files.count() == 1
        assert dialog.files.item(0).text() == "demo.txt"
        assert dialog.hunks.count() == 2
        assert all(dialog.hunks.item(row).checkState() == QtCore.Qt.CheckState.Checked
                   for row in range(dialog.hunks.count()))
        assert dialog.stage_button.isEnabled()
        assert "[x]" in dialog.preview.toPlainText()

        dialog.hunks.item(1).setCheckState(QtCore.Qt.CheckState.Unchecked)
        assert dialog.selected_hunks() == [0]
        assert "[ ]" in dialog.preview.toPlainText()

        staged = []
        dialog.staged.connect(lambda: staged.append(True))
        assert dialog.stage_selected() is True

        assert len(git.patches) == 1
        patch = git.patches[0]
        assert "--- a/demo.txt" in patch
        assert "+line2 changed" in patch
        assert "new tail" not in patch
        assert staged == [True]
        assert any(call[:2] == ["apply", "--cached"] for call in git.calls)
    finally:
        dialog.close()


def test_dialog_marks_untracked_file_as_whole_file_only(tmp_path):
    dialog = DiffReviewDialog(root=str(tmp_path), run_git=FakeGit(diff="", untracked="brand_new.py\n"))
    try:
        assert dialog.files.count() == 1
        dialog.files.setCurrentRow(0)
        assert dialog.file_button.isEnabled()
        assert not dialog.stage_button.isEnabled()
        assert "новый файл" in dialog.file_label.text()
    finally:
        dialog.close()


def test_dialog_without_changes_disables_actions(tmp_path):
    dialog = DiffReviewDialog(root=str(tmp_path), run_git=FakeGit(diff=""))
    try:
        assert dialog.files.count() == 0
        assert not dialog.stage_button.isEnabled()
        assert not dialog.file_button.isEnabled()
    finally:
        dialog.close()
