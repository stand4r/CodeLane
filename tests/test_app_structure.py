"""Tests for app package architecture and problem export features."""
import os
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path for local module resolution
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication

# Ensure offscreen Qt environment for headless tests
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def test_main_reexports():
    """Verify that main.py remains backwards-compatible with all public symbols."""
    import main
    expected_symbols = [
        "main",
        "ApplicationManager",
        "ModernMainWindow",
        "WorkbenchController",
        "WorkbenchTopBar",
        "ExplorerHeader",
        "TITLE_BAR_HEIGHT",
        "UIManager",
        "FileManager",
        "ProjectHandler",
        "CodeRunner",
        "setup_application_style",
        "_load_design_qss",
    ]
    for sym in expected_symbols:
        assert hasattr(main, sym), f"Symbol {sym} missing from main.py"


def test_app_submodules_import():
    """Verify clean imports of all modular app packages."""
    from app import (
        ApplicationManager,
        ModernMainWindow,
        WorkbenchController,
        WorkbenchTopBar,
        ExplorerHeader,
        UIManager,
        FileManager,
        ProjectHandler,
        CodeRunner,
        setup_application_style,
        main,
    )
    from app.styling import _load_design_qss, setup_application_style
    from app.top_bar import TITLE_BAR_HEIGHT

    assert TITLE_BAR_HEIGHT > 0
    assert callable(main)
    assert callable(setup_application_style)


def test_ui_font_resolution():
    """The interface font must resolve to a family the system really has."""
    app = QApplication.instance() or QApplication([])
    from app.styling import (UI_FONT_CANDIDATES, is_font_available, resolve_mono_font,
                             resolve_ui_font, ui_font_family)

    assert is_font_available("NoSuchFont12345") is False
    family = resolve_ui_font()
    assert family, "resolve_ui_font должен вернуть имя семейства"
    # Whatever is returned must be a real installed family (or the final fallback).
    assert family == "Segoe UI" or is_font_available(family)
    assert ui_font_family() == family
    assert resolve_mono_font()
    assert UI_FONT_CANDIDATES[0] == "Segoe UI Variable Text"
    assert "Bahnschrift" not in UI_FONT_CANDIDATES


def test_theme_uses_font_tokens():
    """The interface font is set by QApplication, not per-rule in the stylesheet.

    A `font-family` rule in QSS replaces the widget font and dropped the width
    settings, which is what made Bahnschrift look condensed. Only monospace
    families remain in the stylesheet (as tokens), everything else inherits the
    application font.
    """
    theme = (ROOT / "design" / "theme.qss").read_text(encoding="utf-8")
    assert "@mono_font@" in theme
    declarations = [line.split("font-family:", 1)[1] for line in theme.splitlines()
                    if "font-family:" in line]
    assert declarations, "в theme.qss не найдено ни одного font-family"
    for declaration in declarations:
        first = declaration.split(",")[0].strip().strip(";").strip()
        assert "@mono_font@" in first, declaration
    # The UI family must come from application code only.
    assert "@ui_font@" not in theme


def test_ui_titlebar_facade():
    """Verify ui/titlebar.py re-exports properly."""
    from ui.titlebar import WorkbenchTopBar, ExplorerHeader, TITLE_BAR_HEIGHT
    # The compact title bar uses a 40 px baseline.
    assert TITLE_BAR_HEIGHT == 40


def test_window_control_icons_render_after_theme_changes(monkeypatch):
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QWidget

    from app.top_bar import WorkbenchTopBar

    app = QApplication.instance() or QApplication([])
    old_palette = app.palette()
    window = QWidget()
    window.action_quick_open = lambda: None
    window.show_ai_agent = lambda: None
    monkeypatch.setattr(WorkbenchTopBar, "_definitions", lambda self: {})
    bar = WorkbenchTopBar(window)
    try:
        for background, foreground in (("#181818", "#D4D4D4"), ("#F3F3F3", "#1F1F1F")):
            palette = QPalette(old_palette)
            palette.setColor(QPalette.ColorRole.Window, QColor(background))
            palette.setColor(QPalette.ColorRole.WindowText, QColor(foreground))
            app.setPalette(palette)
            bar.on_ui_scale_changed(100)
            for button in (bar.minimize_button, bar.maximize_button, bar.close_button):
                pixmap = button.icon().pixmap(button.iconSize())
                assert not pixmap.isNull()
                image = pixmap.toImage()
                pixels = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
                assert any(pixel.alpha() > 128 for pixel in pixels)
                assert all(
                    (pixel.lightness() >= 128) != (QColor(background).lightness() >= 128)
                    for pixel in pixels if pixel.alpha() > 128
                )
    finally:
        app.setPalette(old_palette)
        window.close()


def test_workbench_scale_uses_logical_screen_size():
    from types import SimpleNamespace

    from PyQt6.QtCore import QRect
    from app.ui_scale import effective_scale

    def screen(width, height):
        return SimpleNamespace(geometry=lambda: QRect(0, 0, width, height))

    assert effective_scale(screen(1366, 768)) == 100
    assert effective_scale(screen(1920, 1080)) == 100
    assert effective_scale(screen(3440, 1080)) == 100
    assert effective_scale(screen(2560, 1440)) == 133
    assert effective_scale(screen(3840, 2160)) == 150
    assert effective_scale(screen(2560, 1440), 125) == 167
    assert effective_scale(None, 125) == 125


def test_console_widget_problems_export():
    """Verify ConsoleWidget problem formatting and export capabilities."""
    app = QApplication.instance() or QApplication([])
    from widgets.ConsoleWidget import ConsoleWidget
    cw = ConsoleWidget()
    assert "Вывод" not in [cw.tab_widget.tabText(i) for i in range(cw.tab_widget.count())]

    # Add sample problems
    cw.add_problem("error", "main.py", 42, "SyntaxError: invalid syntax")
    cw.add_problem("warning", "app.py", 3, "Unused import 'sys'")

    assert cw.problems_list.count() == 2

    # Check copy_problems
    cw.copy_problems()
    clip_text = QApplication.clipboard().text()
    assert "SyntaxError: invalid syntax" in clip_text
    assert "main.py:42" in clip_text

    # Verify methods exist
    assert hasattr(cw, "save_problems_to_file")
    assert hasattr(cw, "_copy_single_problem")


def test_project_intelligence_tips_export():
    """Verify ProjectIntelligenceDashboard tips data collection and formatting."""
    app = QApplication.instance() or QApplication([])
    from widgets.ProjectIntelligenceDashboard import ProjectIntelligenceDashboard
    pid = ProjectIntelligenceDashboard()

    # Tips tree methods exist
    assert hasattr(pid, "_copy_tips")
    assert hasattr(pid, "_save_tips_to_file")
    assert hasattr(pid, "_collect_tips_data")
    assert hasattr(pid, "copy_tips_btn")
    assert hasattr(pid, "save_tips_btn")


def test_autosave_and_cancelled_bulk_close_keep_all_tabs():
    from types import SimpleNamespace
    from app.main_window import ModernMainWindow

    files = [SimpleNamespace(property=lambda _: None) for _ in range(3)]
    group = SimpleNamespace(count=lambda: len(files), widget=files.__getitem__)
    saved, removed, checked = [], [], []

    def confirm(index, widget):
        checked.append(index)
        return index != 1

    window = SimpleNamespace(
        active_tab_widget=group, editor_groups=lambda: [group],
        _save_modified_tab=lambda group, index: saved.append(index) or True,
        recalculate_unsaved_count=lambda: None, _confirm_tab_close=confirm,
        _remove_editor_tab=lambda index, editor: removed.append(index),
    )
    ModernMainWindow.auto_save(window)
    assert saved == [0, 1, 2]
    assert not ModernMainWindow.close_all_editor_tabs(window)
    assert checked == [2, 1] and not removed


def test_failed_save_keeps_editor_open():
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QMessageBox
    from app.main_window import ModernMainWindow

    messages = []
    window = SimpleNamespace(
        has_unsaved_changes=lambda _: True,
        ask_save_before_close=lambda: QMessageBox.StandardButton.Yes,
        status_bar=SimpleNamespace(showMessage=messages.append),
    )
    tab = SimpleNamespace(property=lambda _: None, save_file=lambda: False)
    assert not ModernMainWindow._confirm_tab_close(window, 0, tab)
    assert messages


def test_file_manager_decodes_utf16_bom_and_legacy_cyrillic(tmp_path):
    from app.file_manager import FileManager

    text = "Тест"
    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes(text.encode("utf-16"))
    assert not FileManager.__new__(FileManager)._is_binary_file(utf16, ".txt")
    assert FileManager._read_text(utf16)[0] == text
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(text.encode("cp1251"))
    assert FileManager._read_text(legacy)[0] == text


def test_project_handler_normalize_root():
    """Verify ProjectHandler _normalize_root handles configs, dicts, and paths."""
    from app.project_handler import ProjectHandler
    handler = ProjectHandler(None)
    assert hasattr(handler, "_normalize_root")
    assert hasattr(handler, "on_project_opened")

    class DummyCfg:
        root_path = "."

    assert handler._normalize_root(DummyCfg()) == str(Path(".").resolve())
    assert handler._normalize_root({"root": "."}) == str(Path(".").resolve())
    assert handler._normalize_root(".") == str(Path(".").resolve())
    assert handler._normalize_root(None) == ""


def test_user_overrides_never_paint_text_surfaces():
    """User overrides must not give text widgets an opaque background.

    The block is appended to ``design/theme.qss`` and therefore wins against
    equal-specificity rules. A blanket ``QWidget { background: ... }`` repaints
    every label and tool button with the window colour, which showed up as
    rectangles whose background did not match the card they sat on (Git panel,
    AI bubbles, analysis cards).
    """
    from app.styling import user_appearance_qss
    qss = user_appearance_qss({'app_background': '#17191C',
                               'text_primary': '#CCCCCC',
                               'editor_background': '#1E1E1E'}, 13)
    text_selectors = {"QWidget", "QLabel", "QToolButton", "QCheckBox",
                      "QRadioButton", "QGroupBox", "QAbstractButton"}
    checked = 0
    for block in qss.split("}"):
        if "{" not in block:
            continue
        selectors, declarations = block.split("{", 1)
        names = {part.strip().split(":")[0].strip()
                 for part in selectors.replace("\n", " ").split(",")}
        if names & text_selectors:
            checked += 1
            assert "background" not in declarations, block.strip()
    assert checked, "не найдено ни одного правила, управляющего текстовыми виджетами"


if __name__ == "__main__":
    test_main_reexports()
    print("test_main_reexports: PASS")
    test_app_submodules_import()
    print("test_app_submodules_import: PASS")
    test_ui_titlebar_facade()
    print("test_ui_titlebar_facade: PASS")
    test_ui_font_resolution()
    print("test_ui_font_resolution: PASS")
    test_theme_uses_font_tokens()
    print("test_theme_uses_font_tokens: PASS")
    test_console_widget_problems_export()
    print("test_console_widget_problems_export: PASS")
    test_project_intelligence_tips_export()
    print("test_project_intelligence_tips_export: PASS")
    test_project_handler_normalize_root()
    print("test_project_handler_normalize_root: PASS")
    test_user_overrides_never_paint_text_surfaces()
    print("test_user_overrides_never_paint_text_surfaces: PASS")
    print("\nALL ARCHITECTURE AND EXPORT TESTS PASSED SUCCESSFULLY!")


def test_run_target_keeps_debug_and_shared_compiler_on_run_channel(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app import code_runner

    run_file = Mock()
    window = SimpleNamespace(
        current_project=None,
        get_current_editor=lambda: None,
        load_launch_arguments=lambda path: "--flag",
        terminal_visible=True,
        console_widget=SimpleNamespace(
            set_current_tab=Mock(), get_terminal=lambda: SimpleNamespace(run_file=run_file)
        ),
        status_bar=SimpleNamespace(showMessage=Mock()),
    )
    monkeypatch.setattr(code_runner, "get_settings_manager", lambda: SimpleNamespace(get_setting=lambda *args: {}))
    monkeypatch.setattr(code_runner, "python_interpreter", lambda configured: "python")
    monkeypatch.setattr(code_runner, "resolve_toolchain", lambda key, fallback, settings: fallback)
    build_command = Mock(return_value=("compile-and-run", None))
    monkeypatch.setattr(code_runner, "build_run_command_for_file", build_command)

    runner = code_runner.CodeRunner(window)
    python_file = tmp_path / "example file.py"
    python_file.touch()
    runner.run_target(python_file, debug=True)
    run_file.assert_called_once_with(f'"python" -m pdb "{python_file.resolve()}" --flag', tmp_path)
    build_command.assert_not_called()

    run_file.reset_mock()
    c_file = tmp_path / "main.c"
    c_file.touch()
    runner.run_target(c_file)
    build_command.assert_called_once_with(str(c_file.resolve()), "--flag", {})
    run_file.assert_called_once_with("compile-and-run", tmp_path)


def test_file_lookup_distinguishes_html_preview_and_editor_in_split_groups(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.file_manager import FileManager

    path = str((tmp_path / "index.html").resolve())
    preview = SimpleNamespace(file_path=path, property=lambda name: name == "pyscribeHtmlPreview")
    editor = SimpleNamespace(file_path=path, property=lambda name: None, get_code=lambda: "")
    preview_group = SimpleNamespace(count=lambda: 1, widget=lambda index: preview, setCurrentIndex=Mock())
    editor_group = SimpleNamespace(count=lambda: 1, widget=lambda index: editor, setCurrentIndex=Mock())
    window = SimpleNamespace(
        app_manager=None, editor_groups=lambda: [preview_group, editor_group], _set_active_group=Mock()
    )

    manager = FileManager(window)
    assert manager._find_and_focus_open_tab(path)
    editor_group.setCurrentIndex.assert_called_once_with(0)
    preview_group.setCurrentIndex.assert_not_called()
    window._set_active_group.assert_called_once_with(editor_group)
    assert manager._find_text_tab(path) == (editor_group, 0, editor)
