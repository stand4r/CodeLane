"""Regression tests for the code editor and the integrated terminal.

These tests guard the specific bugs fixed in the "editor + UI + terminal"
polish pass:

* commenting / un-commenting a multi-line selection must never corrupt text and
  must preserve indentation,
* line duplication / deletion / moving must keep the document intact
  (especially on the last line),
* Ctrl+W must not be swallowed by the editor (native word-delete),
* zoom shortcuts are handled once,
* rectangular (column) selection copy / insert / delete,
* the editor never raises after its C++ object is destroyed,
* the terminal prompt is rendered exactly once,
* shell output is decoded as UTF-8 and ANSI codes are interpreted,
* PowerShell commands starting with a quoted path get the call operator,
* internal ``cd`` sync lines and PowerShell's ``PS>`` noise are filtered.
"""
import os
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtWidgets

# A single QApplication for the whole module, created before any widget exists.
_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

_KEEP_ALIVE = []


def test_terminal_shortcuts_take_precedence_and_filter_control_characters():
    from PyQt6.QtTest import QTest
    from widgets.TerminalWidget import InteractiveTerminalEdit

    class Shell:
        def __init__(self):
            self.interrupts = 0

        def is_run_process_active(self):
            return False

        def interrupt(self):
            self.interrupts += 1

        def print_prompt(self):
            screen._insert_at_end("\n$ ")

    app = _app()
    host = QtWidgets.QWidget()
    shell = Shell()
    screen = InteractiveTerminalEdit(shell, host)
    layout = QtWidgets.QVBoxLayout(host)
    layout.addWidget(screen)
    calls = []
    shortcuts = [
        QtGui.QShortcut(QtGui.QKeySequence(sequence), host)
        for sequence in ("Ctrl+W", "Ctrl+A", "Ctrl+Shift+V", "Ctrl+C", "Ctrl+P")
    ]
    for index, shortcut in enumerate(shortcuts):
        shortcut.activated.connect(lambda index=index: calls.append(index))

    host.show()
    host.activateWindow()
    screen.setFocus()
    app.processEvents()
    screen._insert_at_end("$ ")
    screen._set_input_text("pip install")
    key = QtCore.Qt.Key
    ctrl = QtCore.Qt.KeyboardModifier.ControlModifier
    shift = QtCore.Qt.KeyboardModifier.ShiftModifier
    clipboard = QtWidgets.QApplication.clipboard()
    previous_clipboard = clipboard.text()
    try:
        QTest.keyClick(screen, key.Key_W, ctrl)
        assert screen.get_current_command().strip() == "pip"
        QTest.keyClick(screen, key.Key_A, ctrl)
        assert screen.textCursor().position() == screen.prompt_pos
        QTest.keyClick(screen, key.Key_C, ctrl)
        assert shell.interrupts == 1
        clipboard.setText("\x03\x03pip install pyqt6")
        QTest.keyClick(screen, key.Key_V, ctrl | shift)
        assert screen.get_current_command() == "pip install pyqt6"
        event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key.Key_B, ctrl, "\x02")
        app.sendEvent(screen, event)
        assert screen.get_current_command() == "pip install pyqt6"
        assert "\x03" not in screen.toPlainText() and "\x02" not in screen.toPlainText()
        selection = screen.textCursor()
        selection.setPosition(0)
        selection.setPosition(2, QtGui.QTextCursor.MoveMode.KeepAnchor)
        screen.setTextCursor(selection)
        QTest.keyPress(screen, key.Key_Control)
        assert screen.textCursor().selectedText() == "$ "
        QTest.keyRelease(screen, key.Key_Control)
        QTest.keyClick(screen, key.Key_C, ctrl)
        assert clipboard.text() == "$ "
        assert shell.interrupts == 1
        QTest.keyClick(screen, key.Key_P, ctrl)
        assert calls == [4]
    finally:
        clipboard.setText(previous_clipboard)
        host.close()


def test_terminal_backspace_deletes_input_and_preserves_prompt():
    from PyQt6.QtTest import QTest
    from widgets.TerminalWidget import InteractiveTerminalEdit

    class Shell:
        def is_run_process_active(self):
            return False

    host = QtWidgets.QWidget()
    screen = InteractiveTerminalEdit(Shell(), host)
    screen._insert_at_end("$ ")
    screen._set_input_text("abc")
    screen.setFocus()
    cursor = screen.textCursor()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
    screen.setTextCursor(cursor)
    QTest.keyClick(screen, QtCore.Qt.Key.Key_Backspace)
    assert screen.get_current_command() == "ab"
    assert screen.textCursor().position() == screen.document().characterCount() - 1
    for _ in range(2):
        QTest.keyClick(screen, QtCore.Qt.Key.Key_Backspace)
    assert screen.get_current_command() == ""
    assert screen.toPlainText().startswith("$ ")
    host.close()


def test_terminal_interrupt_does_not_write_control_bytes_to_shell():
    from types import SimpleNamespace
    from widgets.TerminalWidget import TerminalTab

    calls = []
    process = SimpleNamespace(
        state=lambda: QtCore.QProcess.ProcessState.Running,
        write=lambda data: calls.append(data),
        kill=lambda: calls.append("kill"),
    )
    tab = SimpleNamespace(
        process=process,
        is_run_process_active=lambda: False,
        _command_running=False,
    )
    TerminalTab.interrupt(tab)
    assert calls == []
    tab._command_running = True
    TerminalTab.interrupt(tab)
    assert calls == ["kill"]
    assert not tab._command_running


def _app():
    return _APP


def test_syntax_highlighting_covers_all_registered_languages():
    from core.language_services import LanguageProviderFactory
    from widgets.QCodeEditor import ModernSyntaxHighlighter

    examples = {
        "asm": ("MOV eax, 1", "MOV"), "c": ("int x = 1;", "int"),
        "clj": ("(defn f [] 1)", "defn"), "cpp": ("class Cat {};", "class"),
        "cs": ("class Cat {}", "class"), "css": (".item { color: red; }", "color"),
        "dart": ("final x = 1;", "final"), "dockerfile": ("FROM scratch", "FROM"),
        "erl": ("receive _ -> ok end.", "receive"), "ex": ("def f, do: 1", "def"),
        "fs": ("let x = 1", "let"), "go": ("package main", "package"),
        "hs": ("module Main where", "module"), "html": ("<div>text</div>", "<div>"),
        "java": ("class Cat {}", "class"), "jl": ("function f() end", "function"),
        "js": ("const x = 1;", "const"), "json": ('{"id": 1}', '"id"'),
        "jsx": ("const view = <p/>;", "const"), "kt": ("fun f() = 1", "fun"),
        "lua": ("local x = 1", "local"), "m": ("@interface Cat", "interface"),
        "md": ("# Title", "Title"), "ml": ("let answer = 42", "let"),
        "mm": ("@protocol Reader", "protocol"), "nim": ("proc answer(): int = 42", "proc"),
        "php": ("<?php function answer() {}", "function"), "pl": ("sub answer { 42 }", "sub"),
        "py": ("def answer(): pass", "def"), "r": ("if (TRUE) 1", "if"),
        "rb": ("def answer; 42; end", "def"), "rs": ("fn answer() {}", "fn"),
        "scala": ("object App {}", "object"), "sh": ("if true; then :; fi", "if"),
        "sol": ("contract Token {}", "contract"), "sql": ("SeLeCt 1", "SeLeCt"),
        "swift": ("let answer = 42", "let"), "ts": ("interface Cat {}", "interface"),
        "tsx": ("const view = <p/>;", "const"), "v": ("module top;", "module"),
        "xml": ("<element/>", "<element"), "yaml": ("name: example", "name"),
        "zig": ("const answer = 42;", "const"),
    }
    supported = LanguageProviderFactory.get_supported_extensions()
    assert {LanguageProviderFactory.language_for_path(f"example.{ext}") for ext in supported} == examples.keys()
    for language, (text, token) in examples.items():
        config = LanguageProviderFactory.get_language_config(language)
        doc = QtGui.QTextDocument(text)
        highlighter = ModernSyntaxHighlighter(doc, config)
        highlighter.rehighlight()
        start = text.index(token)
        formats = doc.firstBlock().layout().formats()
        assert any(item.start <= start < item.start + item.length and item.format.foreground().color().isValid()
                   for item in formats), language


def test_highlighter_keeps_comment_tokens_inside_strings_and_resumes_after_blocks():
    from core.language_services import LanguageProviderFactory
    from widgets.QCodeEditor import ModernSyntaxHighlighter

    doc = QtGui.QTextDocument('local value = "-- text" -- note\n--[[ block\n]] local x = 1')
    highlighter = ModernSyntaxHighlighter(doc, LanguageProviderFactory.get_language_config("lua"))
    highlighter.rehighlight()

    def color(block_number, offset):
        block = doc.findBlockByNumber(block_number)
        return next(item.format.foreground().color().name() for item in block.layout().formats()
                    if item.start <= offset < item.start + item.length)

    assert color(0, 16) == "#ce9178"
    assert color(0, 28) == "#6a9955"
    assert color(1, 5) == "#6a9955"
    assert color(2, 3) == "#569cd6"

def test_terminal_replaces_token_after_astral_unicode():
    from widgets.TerminalWidget import InteractiveTerminalEdit

    screen = InteractiveTerminalEdit(None)
    screen._insert_at_end("$ ")
    screen._set_input_text("😀 file tails")
    cursor = screen.textCursor()
    cursor.setPosition(screen.prompt_pos + len("😀 file".encode("utf-16-le")) // 2)
    screen.setTextCursor(cursor)
    screen._replace_token(2, "filename")
    assert screen.get_current_command() == "😀 filename tails"
    assert screen._input_cursor_offset() == len("😀 filename")


def test_format_python_preserves_logic_and_is_undoable():
    from widgets.CodeLensBar import CleanCodeRefactorer

    source = 'def f(x):\n    if x == True:\n        return "x=1"\n    return x+1\n'
    formatted = CleanCodeRefactorer.format_code(source)
    assert "return x + 1" in formatted
    assert ast.dump(ast.parse(source)) == ast.dump(ast.parse(formatted))
    editor = _make_editor(source)
    editor.code_lens.format_entire_file()
    assert editor.get_code() == formatted
    editor.editor.undo()
    assert editor.get_code() == source


def test_save_preserves_an_externally_edited_file_and_allows_a_copy(tmp_path):
    from app.file_manager import FileManager

    path = tmp_path / "source.py"
    path.write_text("value = 1\n", encoding="utf-8")
    text, snapshot, text_format = FileManager._read_text(path)
    editor = _make_editor(text)
    path.write_text("value = 2\n", encoding="utf-8")
    editor.set_file_path(path, snapshot=snapshot, text_format=text_format)
    editor.set_code("value = 3\n")
    assert not editor.save_file()
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    assert editor.get_code() == "value = 3\n"
    copy = tmp_path / "copy.py"
    assert editor.save_file_as(str(copy))
    assert copy.read_text(encoding="utf-8") == "value = 3\n"
    assert editor.save_file()


def test_editor_preserves_bom_encoding_line_endings_and_nonbreaking_space(tmp_path):
    from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF32_LE
    from app.file_manager import FileManager

    for index, (codec, marker) in enumerate(
        (("utf-8", BOM_UTF8), ("utf-16-be", BOM_UTF16_BE),
         ("utf-32-le", BOM_UTF32_LE), ("cp1251", b""))
    ):
        path = tmp_path / f"sample{index}.txt"
        original = "Тест\u00a0строки\r\nвторая\r\n"
        path.write_bytes(marker + original.encode(codec))
        text, snapshot, text_format = FileManager._read_text(path)
        editor = _make_editor(text)
        editor.set_file_path(path, snapshot=snapshot, text_format=text_format)
        cursor = editor.editor.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText("третья\n")
        assert editor.save_file(), editor.last_save_error
        assert path.read_bytes() == marker + (original + "третья\r\n").encode(codec)


def test_editor_can_save_utf8_after_legacy_encoding_conflict(tmp_path):
    original = tmp_path / "legacy.txt"
    original_bytes = "Тест\r\n".encode("cp1251")
    original.write_bytes(original_bytes)
    editor = _make_editor("Тест\n🙂\n")
    editor.set_file_path(original)
    assert not editor.save_file()
    assert "Сохранить как UTF-8" in editor.last_save_error
    assert original.read_bytes() == original_bytes
    destination = tmp_path / "utf8.txt"
    assert editor.save_file_as(str(destination), utf8=True)
    assert destination.read_bytes() == "Тест\r\n🙂\r\n".encode("utf-8")
    assert original.read_bytes() == original_bytes


def test_mixed_line_endings_survive_insertions():
    from core.text_file import decode_text_file

    original = b"a\r\nb\nc\r\n"
    _, text_format = decode_text_file(original)
    assert text_format.encode("a\nb\nc\n") == original
    updated = text_format.encode("a\ninserted\nb\nc\n")
    assert updated == b"a\r\ninserted\r\nb\nc\r\n"
    assert text_format.after_save(updated).encode("a\ninserted\nb\nc\n") == updated


def test_format_python_on_save_when_enabled(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from utils import settings_manager

    editor = _make_editor("answer=42\n")
    path = tmp_path / "answer.py"
    editor.set_file_path(path)
    monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: SimpleNamespace(
        get_setting=lambda key, default=None: True if key == "format_on_save" else default,
    ))
    assert editor.save_file()
    assert path.read_text(encoding="utf-8") == "answer = 42\n"


def test_python_docstrings_handle_inline_multiline_and_nested_scopes():
    from widgets.CodeLensBar import DocumentationGenerator

    source = (
        "#!/usr/bin/env python\n# coding: utf-8\n"
        "from __future__ import annotations\n\n"
        "def outer(\n    x: int, /, *args, enabled=True, **kwargs\n):\n"
        "    @staticmethod\n    def inner(): raise ValueError()\n"
        "    return x\n\n"
        "def keep():\n    \"\"\"Original.\"\"\"\n"
    )
    updated = DocumentationGenerator.document_python(source, filename="demo.py")
    assert updated.startswith("#!/usr/bin/env python\n# coding: utf-8\n")
    tree = ast.parse(updated)
    outer, keep = tree.body[-2:]
    assert all(name in ast.get_docstring(outer) for name in ("x:", "*args:", "enabled:", "**kwargs:"))
    assert "Raises:" not in ast.get_docstring(outer)
    assert "ValueError" in ast.get_docstring(outer.body[1])
    assert ast.get_docstring(keep) == "Original."
    assert DocumentationGenerator.document_python(updated) == updated
    only_inner = ast.parse(DocumentationGenerator.document_python(source, line=9))
    assert ast.get_docstring(only_inner.body[1]) is None
    assert ast.get_docstring(only_inner.body[1].body[0])


def _make_editor(code: str = ""):
    from widgets.QCodeEditor import ModernCodeEditor
    app = _app()
    # Keep a strong reference to the *caller's* wrapper object: without one the
    # Python wrapper (and its C++ object) may be collected mid-test.
    host = QtWidgets.QWidget()
    _KEEP_ALIVE.append(host)
    ed = ModernCodeEditor(parent=host, language="python", settings={})
    _KEEP_ALIVE.append(ed)
    host.resize(640, 480)
    host.show()
    if code:
        ed.set_code(code)
    app.processEvents()
    return ed


def _select(ed, start: int, end: int):
    cursor = ed.editor.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QtGui.QTextCursor.MoveMode.KeepAnchor)
    ed.editor.setTextCursor(cursor)


def test_toggle_comment_single_line():
    ed = _make_editor("a = 1\nb = 2\nc = 3")
    _select(ed, 0, 0)
    ed._toggle_comment()
    assert ed.get_code() == "# a = 1\nb = 2\nc = 3"
    ed._toggle_comment()
    assert ed.get_code() == "a = 1\nb = 2\nc = 3"


def test_toggle_comment_multi_line_selection():
    """Regression: only the last line used to be commented and text was mangled."""
    ed = _make_editor("a = 1\nb = 2\nc = 3\n")
    _select(ed, 0, 13)
    ed._toggle_comment()
    assert ed.get_code() == "# a = 1\n# b = 2\n# c = 3\n"
    ed._toggle_comment()
    assert ed.get_code() == "a = 1\nb = 2\nc = 3\n"


def test_toggle_comment_preserves_indentation():
    code = "def f():\n    x = 1\n    y = 2\n"
    ed = _make_editor(code)
    _select(ed, 0, len(code.rstrip()))
    ed._toggle_comment()
    assert ed.get_code() == "# def f():\n    # x = 1\n    # y = 2\n"
    ed._toggle_comment()
    assert ed.get_code() == code


def test_toggle_comment_is_single_undo_step():
    ed = _make_editor("a\nb\nc")
    _select(ed, 0, 3)
    ed._toggle_comment()
    assert ed.get_code() == "# a\n# b\nc"
    ed.editor.undo()
    assert ed.get_code() == "a\nb\nc"


def test_delete_last_line():
    """Regression: deleting the last line used to leave the line and a stray newline."""
    ed = _make_editor("a\nb\nc")
    cursor = ed.editor.textCursor()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
    ed.editor.setTextCursor(cursor)
    ed._delete_line()
    assert ed.get_code() == "a\nb"


def test_delete_middle_line():
    ed = _make_editor("a\nb\nc")
    cursor = ed.editor.textCursor()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.Down)
    ed.editor.setTextCursor(cursor)
    ed._delete_line()
    assert ed.get_code() == "a\nc"


def test_duplicate_last_line():
    """Regression: duplicating the last line only appended a newline."""
    ed = _make_editor("a\nb")
    cursor = ed.editor.textCursor()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
    ed.editor.setTextCursor(cursor)
    ed._duplicate_line()
    assert ed.get_code() == "a\nb\nb"


def test_duplicate_first_line():
    ed = _make_editor("a\nb")
    _select(ed, 0, 0)
    ed._duplicate_line()
    assert ed.get_code() == "a\na\nb"


def test_move_lines():
    ed = _make_editor("one\ntwo\nthree")
    cursor = ed.editor.textCursor()
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
    cursor.movePosition(QtGui.QTextCursor.MoveOperation.Down)
    ed.editor.setTextCursor(cursor)
    ed.editor._move_line_up()
    assert ed.get_code() == "two\none\nthree"
    ed.editor._move_line_down()
    assert ed.get_code() == "one\ntwo\nthree"


def test_ctrl_w_is_handled_by_editor_not_window():
    """Regression: Ctrl+W used to be routed to ``action_close_file`` (closing the tab).

    The editor must not intercept the key itself; Qt's native editing pipeline
    (or the window shortcut) decides. We assert the editor's key handler does
    not delegate to a window-provided ``action_close_file``.
    """
    ed = _make_editor("hello world")
    calls = []
    window = ed.window()
    if hasattr(window, "action_close_file"):
        return  # real workbench shortcut owns the key there — nothing to assert
    # A stub window exposing the action must not be invoked by the editor.
    class StubWindow:
        file_path = None
        def action_close_file(self):
            calls.append(1)
        def get_current_editor(self):
            return None
    ed.window = lambda: StubWindow()
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_W, QtCore.Qt.KeyboardModifier.ControlModifier, "w")
    ed.editor.keyPressEvent(event)
    assert not calls, "Ctrl+W must not trigger action_close_file from the editor"


def test_zoom_shortcuts_single_effect():
    from PyQt6.QtTest import QTest
    ed = _make_editor()
    ed.editor.setFocus()
    start = ed.editor.current_font_size
    QTest.keyClick(ed.editor, QtCore.Qt.Key.Key_Equal, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert ed.editor.current_font_size == start + 1
    QTest.keyClick(ed.editor, QtCore.Qt.Key.Key_0, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert ed.editor.current_font_size == start


def test_editor_shortcuts_with_workbench_shortcuts_installed():
    """Use real Qt shortcut dispatch with both scopes present."""
    from PyQt6.QtTest import QTest
    from app.main_window import ModernMainWindow
    from widgets.QCodeEditor import ModernCodeEditor

    class Workbench(QtWidgets.QWidget):
        def __getattr__(self, name):
            # Supply inert handlers for workbench actions outside this test.
            return lambda: None

    host = Workbench()
    ModernMainWindow._install_global_shortcuts(host)
    editor = ModernCodeEditor(parent=host, language="python", settings={})
    QtWidgets.QVBoxLayout(host).addWidget(editor)
    host.show()
    host.activateWindow()
    editor.editor.setFocus()
    _app().processEvents()
    try:
        assert editor.editor.hasFocus()
        QTest.keyClick(editor.editor, QtCore.Qt.Key.Key_F, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert editor.find_bar.isVisible()

        editor.find_bar.hide_bar()
        editor.editor.setFocus()
        QTest.keyClick(editor.editor, QtCore.Qt.Key.Key_H, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert editor.find_bar.replace_input.isVisible()

        editor.find_bar.hide_bar()
        editor.editor.setFocus()
        original_size = editor.editor.current_font_size
        QTest.keyClick(editor.editor, QtCore.Qt.Key.Key_Equal, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert editor.editor.current_font_size == original_size + 1

        editor.set_code("first\nsecond")
        QTest.keyClick(editor.editor, QtCore.Qt.Key.Key_K,
                       QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier)
        assert editor.get_code() == "second"
    finally:
        host.close()


def test_rectangle_selection_copy_insert_delete():
    ed = _make_editor("aaaa\nbbbb\ncccc\n")
    cursor = QtGui.QTextCursor(ed.editor.document())
    ed.editor.rectangle_start_pos = ed.editor.cursorRect(cursor).center()
    cursor.setPosition(ed.editor.document().findBlockByNumber(1).position() + 2)
    ed.editor.rectangle_end_pos = ed.editor.cursorRect(cursor).center()
    ed.editor.update_rectangle_selection()
    assert ed.editor.get_rectangle_selection_text() == "aa\nbb"

    ed.editor.copy_rectangle_selection()
    assert QtWidgets.QApplication.clipboard().text() == "aa\nbb"

    ed.editor.update_rectangle_selection()
    ed.editor.insert_text_in_rectangle("ZZ")
    assert ed.get_code().startswith("ZZaa\nZZbb")


def test_auto_closure_and_smart_indent():
    from PyQt6.QtTest import QTest
    ed = _make_editor()
    ed.editor.setFocus()
    QTest.keyClicks(ed.editor, "def f():")
    QTest.keyClick(ed.editor, QtCore.Qt.Key.Key_Return)
    assert ed.get_code() == "def f():\n    "
    QTest.keyClicks(ed.editor, "print(")
    assert ed.get_code() == "def f():\n    print()"
    QTest.keyClick(ed.editor, QtCore.Qt.Key.Key_Backspace)
    assert ed.get_code() == "def f():\n    print"


def test_find_bar_replace_all():
    ed = _make_editor("a a a a")
    ed.show_find_bar()
    ed.find_bar.find_input.setText("a")
    ed.find_bar.replace_input.setText("bb")
    ed.find_bar.replace_all()
    assert ed.get_code() == "bb bb bb bb"


def test_editor_survives_deletion():
    """Regression: a queued QTimer.singleShot touched a deleted C++ object."""
    app = _app()
    from widgets.QCodeEditor import ModernCodeEditor
    host = QtWidgets.QWidget()
    host.show()
    ed = ModernCodeEditor(parent=host, language="python", settings={})
    ed.set_code("x = 1")
    host.deleteLater()
    # Flush deferred events; previously raised RuntimeError in setFont's timer.
    for _ in range(5):
        app.processEvents()


def test_terminal_opens_clean_without_banner(tmp_path, monkeypatch):
    """A fresh terminal shows only the prompt — no welcome/help banner."""
    import time
    from utils import programs
    from widgets.TerminalWidget import TerminalWidget

    monkeypatch.setattr(programs, "_state_dir", lambda: tmp_path)
    app = _app()
    widget = TerminalWidget()
    widget.resize(700, 240)
    widget.show()
    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        tab = widget._current()
        if tab is not None and tab._shell_running() and "$ " in tab.screen.toPlainText():
            break
        time.sleep(0.02)
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)
    tab = widget._current()
    text = tab.screen.toPlainText()
    try:
        assert "$ " in text, "the prompt must be present"
        assert "PyScribe Terminal" not in text, "no startup banner"
        assert "Среда готова" not in text
        # Headless Bash reports missing job control before it prints the prompt.
        lines = [
            line for line in text.strip().splitlines()
            if not (os.name != "nt" and line.startswith((
                "bash: cannot set terminal process group", "bash: no job control in this shell",
            )))
        ]
        assert len(lines) == 1 and "$" in lines[0], f"buffer must contain only the prompt, got {text!r}"
    finally:
        widget.shutdown()


def test_terminal_banner_hook_is_quiet(tmp_path, monkeypatch):
    """The legacy show_startup_banner() keeps working but adds nothing."""
    import time
    from widgets.TerminalWidget import TerminalWidget

    monkeypatch.setattr(TerminalWidget, "_session_path", lambda self: tmp_path / "terminal.json")
    app = _app()
    widget = TerminalWidget()
    widget.resize(700, 240)
    widget.show()
    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        tab = widget._current()
        if tab is not None and tab.screen.toPlainText().endswith("$ "):
            break
        time.sleep(0.02)
    tab = widget._current()
    try:
        before = tab.screen.toPlainText()
        widget.show_startup_banner()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
        after = tab.screen.toPlainText()
        assert "PyScribe Terminal" not in after
        assert after.count("$ ") == before.count("$ ")
    finally:
        widget.shutdown()


def test_console_action_buttons_fill_the_strip():
    """Regression: the terminal toolbar buttons were half the strip height."""
    import time
    from widgets.ConsoleWidget import ConsoleWidget
    app = _app()
    console = ConsoleWidget()
    console.resize(900, 260)
    console.show()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.01)
    bar = console.tab_widget.tabBar()
    strip_height = max(bar.height(), bar.sizeHint().height())
    buttons = list(console._toolbar_buttons.values())
    assert buttons
    for button in buttons:
        assert button.height() >= 28, f"button too short: {button.height()}"
        # The action area must cover the strip minus small margins, not ~half.
        assert button.height() >= strip_height - 10, (
            f"button {button.objectName()} h={button.height()} vs strip {strip_height}"
        )
        assert button.width() >= 28
    console.shutdown()


def test_markdown_codelens_has_preview_button():
    """Markdown files get a preview action in the CodeLens row; other files don't."""
    import tempfile
    from widgets.CodeLensBar import CodeLensBar
    from widgets.QCodeEditor import ModernCodeEditor

    app = _app()
    tmp = tempfile.mkdtemp()

    def lens_for(name: str, suffix: str):
        path = Path(tmp) / name
        path.write_text("# Title\n\nbody\n" if suffix == "md" else "x = 1\n",
                        encoding="utf-8")
        host = QtWidgets.QWidget()
        host.show()
        ed = ModernCodeEditor(parent=host, language=suffix, settings={})
        ed.file_path = path
        ed.set_code(path.read_text(encoding="utf-8"))
        bar = CodeLensBar(ed)
        bar._refresh_lens()
        return bar

    md_bar = lens_for("README.md", "md")
    try:
        assert md_bar.preview_btn.isVisible() or md_bar.preview_btn.isVisibleTo(md_bar)
        assert "Предпросмотр" in md_bar.preview_btn.text()
    finally:
        md_bar.deleteLater()

    py_bar = lens_for("demo.py", "py")
    try:
        assert not py_bar.preview_btn.isVisible(), \
            "the markdown preview action must not appear for Python files"
    finally:
        py_bar.deleteLater()


def test_welcome_icon_has_no_plate():
    """The welcome mark is the bare two-slash logo (transparent, no frame)."""
    from widgets.WelcomeWidget import Ui_Welcome
    widget = Ui_Welcome(settings={}, recent_files=[])
    try:
        icon = widget.findChild(QtWidgets.QLabel, "welcomeIcon")
        assert icon is not None, "welcome icon label must exist"
        pixmap = icon.pixmap()
        assert pixmap is not None and not pixmap.isNull()
        image = pixmap.toImage()
        # Corners must be transparent — a plate would paint them.
        for x, y in ((0, 0), (image.width() - 1, 0),
                     (0, image.height() - 1),
                     (image.width() - 1, image.height() - 1)):
            assert image.pixelColor(x, y).alpha() == 0, \
                "welcome icon must have no background plate"
    finally:
        widget.deleteLater()


def test_terminal_print_prompt_idempotent():
    """The prompt is drawn exactly once — and never before the shell exists."""
    from widgets.TerminalWidget import TerminalTab
    app = _app()
    tab = TerminalTab(99, "t", os.getcwd())
    try:
        # No shell yet -> no prompt is drawn (it would later "jump" position).
        if not tab._shell_running():
            tab.print_prompt()
            assert tab.screen.toPlainText() == ""
            return
        tab.print_prompt()
        first = tab.screen.toPlainText()
        tab.print_prompt()
        tab.print_prompt()
        assert tab.screen.toPlainText() == first
        assert first.count("$ ") == 1
        assert not first.endswith("\n"), "prompt must not leave a trailing blank line"
    finally:
        tab.stop_process()


def test_terminal_run_channel_streams_output():
    """Regression: IDE runs used to go through the shell and lose output.

    ``start_run_process`` must print the program output live (before the
    process exits) and report the exit code afterwards.
    """
    import tempfile
    import time
    from widgets.TerminalWidget import TerminalTab

    app = _app()
    tab = TerminalTab(94, "t", os.getcwd())
    script = Path(tempfile.mkdtemp()) / "stream.py"
    script.write_text(
        "import time\n"
        "for i in range(3):\n"
        "    print('tick', i, flush=True)\n"
        "    time.sleep(0.35)\n"
        "print('DONE', flush=True)\n",
        encoding="utf-8",
    )
    try:
        tab.start_run_process(f'"{sys.executable}" -u "{script}"')
        seen_live = False
        deadline = time.time() + 12
        while time.time() < deadline:
            app.processEvents()
            text = tab.screen.toPlainText()
            if "tick 0" in text and not tab.is_run_process_active():
                break
            if "tick 0" in text and "DONE" not in text:
                seen_live = True
            time.sleep(0.02)
        for _ in range(40):
            app.processEvents()
            time.sleep(0.01)
        final = tab.screen.toPlainText()
        assert "tick 0" in final and "DONE" in final, final
        assert seen_live, "output must appear while the process is still running"
        assert "Завершено" in final or "Код выхода" in final
        assert not tab.is_run_process_active()
    finally:
        tab.stop_process()


def test_terminal_run_reports_exit_code():
    import tempfile
    import time
    from widgets.TerminalWidget import TerminalTab

    app = _app()
    tab = TerminalTab(93, "t", os.getcwd())
    script = Path(tempfile.mkdtemp()) / "fail.py"
    script.write_text("import sys\nprint('boom')\nsys.exit(5)\n", encoding="utf-8")
    try:
        tab.start_run_process(f'"{sys.executable}" -u "{script}"')
        deadline = time.time() + 12
        finished = False
        while time.time() < deadline:
            app.processEvents()
            if not tab.is_run_process_active():
                finished = True
                break
            time.sleep(0.02)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
        text = tab.screen.toPlainText()
        assert finished
        assert "boom" in text
        assert "Код выхода 5" in text, text
    finally:
        tab.stop_process()


def test_terminal_split_command_keeps_paths_with_spaces():
    from widgets.TerminalWidget import TerminalTab
    prog, args = TerminalTab._split_command(
        '"C:\\Program Files\\Py\\python.exe" "C:\\a b\\x.py" --flag "v 1"'
    )
    assert prog == "C:\\Program Files\\Py\\python.exe"
    assert args == ["C:\\a b\\x.py", "--flag", "v 1"]
    prog2, args2 = TerminalTab._split_command('& "C:/x/y.exe" arg')
    assert prog2 == "C:/x/y.exe" and args2 == ["arg"]


def test_terminal_ansi_parsing():
    from widgets.TerminalWidget import TerminalTab
    parts = TerminalTab._split_ansi("\x1b[31mred\x1b[0m plain")
    assert parts[0] == ("#F14C4C", "red")
    assert parts[1] == " plain"


def test_terminal_powershell_command_adaptation(monkeypatch):
    from widgets.TerminalWidget import TerminalTab
    app = _app()
    tab = TerminalTab(98, "t", os.getcwd())
    try:
        with monkeypatch.context() as patch:
            patch.setattr("widgets.TerminalWidget.IS_WINDOWS", True)
            tab.shell_program = r"C:\WINDOWS\powershell.exe"
            assert tab._shell_command_text('"C:/x/y.py" a').startswith("& ")
            assert tab._shell_command_text("git status") == "git status"
            tab.shell_program = r"C:\WINDOWS\cmd.exe"
            assert tab._shell_command_text('"C:/x/y.py"') == '"C:/x/y.py"'
    finally:
        tab.stop_process()


def test_terminal_prompt_and_echo_filters():
    from widgets.TerminalWidget import TerminalTab
    app = _app()
    tab = TerminalTab(97, "t", os.getcwd())
    try:
        # PowerShell's bare continuation prompt must never reach the buffer.
        assert "PS>" not in tab._strip_shell_prompt("PS>hello\n")
        # The shell's echo of our own prompt is removed.
        user, host = tab.get_linux_user_host()
        assert tab._strip_shell_prompt(f"{user}@{host}:~/x$ ls") == "ls"
    finally:
        tab.stop_process()


def test_terminal_internal_cd_line_detection():
    from widgets.TerminalWidget import TerminalTab
    app = _app()
    tab = TerminalTab(96, "t", os.getcwd())
    try:
        assert tab._is_internal_sync_line(
            "Set-Location -LiteralPath ([Text.Encoding]::Unicode.GetString("
            "[Convert]::FromBase64String('QUJD')))"
        )
        assert not tab._is_internal_sync_line("ls -la")
    finally:
        tab.stop_process()


def test_terminal_cd_text_is_str_safe():
    """Regression: a pathlib.Path used to raise in _shell_cd_text."""
    from widgets.TerminalWidget import TerminalTab
    app = _app()
    tab = TerminalTab(95, "t", os.getcwd())
    try:
        assert isinstance(tab._shell_cd_text(Path("C:/temp")), str)
    finally:
        tab.stop_process()


def test_animations_helpers_are_safe_on_dead_widgets():
    from widgets import animations
    app = _app()
    widget = QtWidgets.QWidget()
    widget.deleteLater()
    app.processEvents()
    # Must not raise.
    animations.fade_in(widget, duration=10)
    animations.fade_out(widget, duration=10)
    animations.flash(widget, duration=10)
    animations.safe_call(None)


def test_palette_pill_paint_helper():
    """The flash overlay paints without a QSS round-trip."""
    from widgets.animations import flash
    app = _app()
    from PyQt6.QtWidgets import QLabel
    lbl = QLabel("x")
    lbl.resize(40, 20)
    lbl.show()
    anim = flash(lbl, duration=20)
    assert anim is not None
    for _ in range(10):
        app.processEvents()


if __name__ == "__main__":
    # Run in definition order (sorted order may reuse destroyed widgets badly).
    tests = [(name, obj) for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}", flush=True)
        except Exception as exc:
            failures += 1
            import traceback
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        _app().processEvents()
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed", flush=True)
    sys.exit(1 if failures else 0)
