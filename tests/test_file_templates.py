"""Тесты шаблонов новых файлов (core/file_templates.py, widgets/FileTemplateDialog.py)."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.file_templates import (  # noqa: E402
    DEFAULT_TEMPLATE_IDS,
    FileTemplate,
    default_templates,
    detect_author,
    ensure_templates,
    load_templates,
    placeholder_values,
    render_template,
    template_for_path,
    templates_directory,
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt6 import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    return app


# --------------------------------------------------------------------- store
def test_default_templates_cover_required_kinds():
    templates = default_templates()
    ids = {template.id for template in templates}
    assert set(DEFAULT_TEMPLATE_IDS) <= ids
    assert {"python-module", "qt-widget", "pytest-test", "readme"} <= ids
    for template in templates:
        assert template.title
        assert template.body.strip()
    extensions = {template.id: template.extension for template in templates}
    assert extensions["python-module"] == ".py"
    assert extensions["qt-widget"] == ".py"
    assert extensions["pytest-test"] == ".py"
    assert extensions["readme"] == ".md"


def test_ensure_templates_writes_project_files(tmp_path):
    directory = ensure_templates(tmp_path)
    assert directory == tmp_path / ".codelane" / "templates"
    assert directory.is_dir()
    for template in default_templates():
        target = directory / f"{template.id}.json"
        assert target.is_file(), target
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["id"] == template.id
        assert payload["body"] == template.body


def test_ensure_templates_keeps_user_edits(tmp_path):
    directory = ensure_templates(tmp_path)
    custom = directory / "readme.json"
    custom.write_text(
        json.dumps(
            {"id": "readme", "title": "Мой README", "extension": ".md", "body": "# ${name}"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ensure_templates(tmp_path)
    loaded = {template.id: template for template in load_templates(tmp_path)}
    assert loaded["readme"].title == "Мой README"
    assert loaded["readme"].body == "# ${name}"


def test_load_templates_without_project_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    templates = load_templates(None)
    assert {template.id for template in templates} == set(DEFAULT_TEMPLATE_IDS)
    assert not (tmp_path / ".codelane").exists()


def test_load_templates_reads_custom_template(tmp_path):
    directory = templates_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "custom.json").write_text(
        json.dumps(
            {
                "id": "custom",
                "title": "Свой шаблон",
                "description": "Описание",
                "extension": ".sql",
                "body": "-- ${name} ${author}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = {template.id: template for template in load_templates(tmp_path)}
    assert "custom" in loaded
    assert loaded["custom"].extension == ".sql"
    # Встроенные шаблоны тоже доступны рядом с пользовательским.
    assert set(DEFAULT_TEMPLATE_IDS) <= set(loaded)


def test_load_templates_skips_broken_file(tmp_path):
    directory = ensure_templates(tmp_path)
    (directory / "broken.json").write_text("{ not a json", encoding="utf-8")
    loaded = {template.id for template in load_templates(tmp_path)}
    assert "broken" not in loaded
    assert set(DEFAULT_TEMPLATE_IDS) <= loaded


# ------------------------------------------------------------------ rendering
def test_placeholder_values_for_file():
    values = placeholder_values(
        "src/my_tool.py",
        root="/project/demo",
        author="Дмитрий",
        today=date(2026, 2, 17),
    )
    assert values["name"] == "my_tool"
    assert values["file_name"] == "my_tool.py"
    assert values["module"] == "my_tool"
    assert values["subject"] == "my_tool"
    assert values["class_name"] == "MyTool"
    assert values["project"] == "demo"
    assert values["date"] == "17.02.2026"
    assert values["year"] == "2026"
    assert values["author"] == "Дмитрий"


def test_placeholder_subject_strips_test_prefix():
    values = placeholder_values("tests/test_http_client.py", author="A", today=date(2026, 1, 1))
    assert values["module"] == "test_http_client"
    assert values["subject"] == "http_client"


def test_render_template_substitutes_placeholders():
    template = FileTemplate(
        id="demo",
        title="Демо",
        body='"""${project}: ${module} (${class_name})."""\n# ${date} ${year} ${author}\n',
    )
    text = render_template(
        template,
        "/project/demo/src/http_client.py",
        root="/project/demo",
        author="Дмитрий",
        today=date(2026, 2, 17),
    )
    assert text == '"""demo: http_client (HttpClient)."""\n# 17.02.2026 2026 Дмитрий\n'


def test_render_template_keeps_python_braces():
    template = FileTemplate(
        id="demo",
        title="Демо",
        body='value = f"{name}: ${name}"\n',
    )
    text = render_template(template, "notes.py", author="A", today=date(2026, 1, 1))
    assert text == 'value = f"{name}: notes"\n'


def test_render_template_keeps_unknown_placeholder():
    template = FileTemplate(id="demo", title="Демо", body="${unknown} ${name}")
    text = render_template(template, "abc.py", author="A", today=date(2026, 1, 1))
    assert text == "${unknown} abc"


def test_render_template_blank_returns_empty():
    assert render_template(None, "abc.py") == ""


def test_template_for_path_prefers_matching_kind():
    templates = default_templates()
    assert template_for_path(templates, "src/widget.py").id == "python-module"
    assert template_for_path(templates, "tests/test_widget.py").id == "pytest-test"
    assert template_for_path(templates, "README.md").id == "readme"
    assert template_for_path(templates, "notes.txt") is None


# --------------------------------------------------------------------- author
def test_detect_author_uses_git(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = "  Дмитрий\n"

    def fake_run(*_args, **_kwargs):
        return Result()

    monkeypatch.setattr("core.file_templates.run_hidden", fake_run)
    assert detect_author(tmp_path) == "Дмитрий"


def test_detect_author_falls_back_to_env(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("core.file_templates.run_hidden", fake_run)
    monkeypatch.setenv("USERNAME", "tester")
    assert detect_author(Path.cwd()) == "tester"


def test_detect_author_last_resort(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("core.file_templates.run_hidden", fake_run)
    for variable in ("GIT_AUTHOR_NAME", "USERNAME", "USER", "LOGNAME"):
        monkeypatch.delenv(variable, raising=False)
    assert detect_author(None) == "автор"


# ------------------------------------------------------------------------ UI
def test_dialog_preselects_template_by_extension(qapp):
    from widgets.FileTemplateDialog import FileTemplateDialog

    dialog = FileTemplateDialog(
        default_templates(), file_path=Path("tests/test_demo.py"), author="Дмитрий"
    )
    selected = dialog._current_template()
    assert selected is not None and selected.id == "pytest-test"
    preview = dialog.preview.toPlainText()
    assert "def test_demo_smoke" in preview
    assert "Дмитрий" in preview
    dialog.deleteLater()


def test_dialog_blank_option_creates_empty_file(qapp):
    from widgets.FileTemplateDialog import FileTemplateDialog

    dialog = FileTemplateDialog(default_templates(), file_path=Path("notes.txt"))
    dialog.list.setCurrentRow(0)
    dialog._accept_current()
    assert dialog.selected_template is None
    assert dialog.preview.toPlainText() == "Файл будет создан пустым."
    assert dialog.template_body() == ""
    dialog.deleteLater()


def test_dialog_preview_for_python_module(qapp):
    from widgets.FileTemplateDialog import FileTemplateDialog

    dialog = FileTemplateDialog(
        default_templates(),
        file_path=Path("src/http_client.py"),
        root=None,
        author="Дмитрий",
    )
    assert dialog._current_template().id == "python-module"
    preview = dialog.preview.toPlainText()
    assert "http_client" in preview
    assert "def run()" in preview
    assert "${name}" not in preview
    dialog.deleteLater()


def test_ask_template_cancel_returns_none(qapp, monkeypatch):
    from PyQt6 import QtWidgets

    from widgets import FileTemplateDialog as module

    monkeypatch.setattr(
        module.FileTemplateDialog,
        "exec",
        lambda self: QtWidgets.QDialog.DialogCode.Rejected,
    )
    assert module.ask_template(None, default_templates(), file_path="a.py") is None


def test_ask_template_accept_returns_body(qapp, monkeypatch):
    from PyQt6 import QtWidgets

    from widgets import FileTemplateDialog as module

    def fake_exec(self):
        self.list.setCurrentRow(1)  # первый реальный шаблон
        self._accept_current()
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.FileTemplateDialog, "exec", fake_exec)
    body = module.ask_template(
        None, default_templates(), file_path="src/demo.py", author="Дмитрий"
    )
    assert "demo" in body
    assert "def run()" in body
