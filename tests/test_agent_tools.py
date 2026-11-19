"""Tests for the agent's project tools (`services/agent_tools.py`).

Run: python tests/test_agent_tools.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.agent_tools import TOOL_DEFINITIONS, LocalToolbox, ToolError


def _make_project(root: Path) -> None:
    (root / "sub").mkdir()
    (root / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
    (root / "sub" / "notes.txt").write_text("черновик\n", encoding="utf-8")


def _toolbox(root, **kwargs):
    approvals = []

    def confirm(kind, title, details):
        approvals.append(kind)
        return True

    box = LocalToolbox(str(root), confirm=confirm, **kwargs)
    return box, approvals


def test_read_file_returns_content():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, approvals = _toolbox(root)
        text = box.run("read_file", json.dumps({"path": "app.py"}))
        assert "def greet" in text
        assert approvals == []          # read-only tools never ask


def test_read_file_prefers_the_open_unsaved_editor_buffer():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box = LocalToolbox(str(root), ui_action=lambda kind, payload:
                           "unsaved = True\n" if kind == "read_buffer" else "")
        assert box.read_file("app.py") == "unsaved = True\n"


def test_open_file_routes_to_the_ide_callback():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        calls = []
        box = LocalToolbox(str(root), access_mode="read_only",
                           ui_action=lambda kind, payload:
                           calls.append((kind, payload)) or "opened")
        assert box.run("open_file", json.dumps({"path": "app.py"})) == "opened"
        assert calls[0][0] == "open_file"
        assert Path(calls[0][1]["path"]).name == "app.py"


def test_edit_file_replaces_only_a_unique_exact_fragment():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        calls = []
        box, approvals = _toolbox(root, access_mode="write_only",
                                  ui_action=lambda kind, payload:
                                  (calls.append((kind, payload)) or
                                   ("__NO_OPEN_EDITOR_BUFFER__" if kind == "read_buffer"
                                    else "editor updated")))
        original = (root / "app.py").read_text(encoding="utf-8")
        result = box.run("edit_file", json.dumps({
            "path": "app.py", "old_text": "return 'hi ' + name",
            "new_text": "return f'hello {name}'"}))
        assert result == "editor updated"
        assert approvals == ["edit_file"]
        assert calls[-1][0] == "edit_file"
        assert calls[-1][1]["expected"] == original
        assert (root / "app.py").read_text(encoding="utf-8") == original


def test_edit_file_uses_the_unsaved_buffer_as_its_expected_version():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        original = (root / "app.py").read_text(encoding="utf-8")
        unsaved = original.replace("hi ", "hello ")
        calls = []

        def ui_action(kind, payload):
            calls.append((kind, payload))
            return unsaved if kind == "read_buffer" else "applied in editor"

        box = LocalToolbox(str(root), access_mode="full", ui_action=ui_action)
        result = box.edit_file("app.py", "return 'hello ' + name",
                               "return f'hello {name}'")
        assert result == "applied in editor"
        assert calls[-1][1]["expected"] == unsaved
        assert (root / "app.py").read_text(encoding="utf-8") == original


def test_edit_file_rejects_non_unique_fragments():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
        box = LocalToolbox(str(root), access_mode="full")
        try:
            box.edit_file("dup.txt", "same", "changed")
        except ToolError as exc:
            assert "ровно один раз" in str(exc)
        else:
            raise AssertionError("неоднозначная правка была применена")


def test_paths_outside_project_are_rejected():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, _ = _toolbox(root)
        for payload in ({"path": "../secret.txt"}, {"path": "C:/Windows/win.ini"}):
            try:
                box.run("read_file", json.dumps(payload))
            except ToolError:
                continue
            raise AssertionError(f"путь вне проекта разрешён: {payload}")


def test_write_file_asks_in_restricted_mode_and_writes():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, approvals = _toolbox(root, access_mode="write_only")
        result = box.run("write_file", json.dumps({
            "path": "sub/made.txt", "content": "готово"}))
        assert "Создан" in result
        assert (root / "sub" / "made.txt").read_text(encoding="utf-8") == "готово"
        assert approvals == ["write_file"]


def test_full_access_writes_without_any_prompt():
    """«Полный доступ» must not ask: that was the reason edits never happened."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, approvals = _toolbox(root, access_mode="full")
        result = box.run("write_file", json.dumps({
            "path": "sub/free.txt", "content": "без диалога"}))
        assert "Создан" in result
        assert approvals == [], "полный доступ не должен спрашивать подтверждение"
        assert (root / "sub" / "free.txt").read_text(encoding="utf-8") == "без диалога"


def test_rejected_write_leaves_file_untouched():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box = LocalToolbox(str(root), confirm=lambda *a: False, access_mode="write_only")
        try:
            box.run("write_file", json.dumps({"path": "app.py", "content": "hack"}))
            raise AssertionError("запись без подтверждения прошла")
        except ToolError as exc:
            assert "отклонил" in str(exc)
        assert "def greet" in (root / "app.py").read_text(encoding="utf-8")


def test_permissions_can_disable_tools():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box = LocalToolbox(str(root), confirm=lambda *a: True,
                           allow_write=False, allow_commands=False)
        names = [d["function"]["name"] for d in box.definitions()]
        assert "write_file" not in names
        assert "run_terminal_command" not in names
        assert "read_file" in names
        try:
            box.run("write_file", json.dumps({"path": "x.txt", "content": "y"}))
            raise AssertionError("запрещённая запись прошла")
        except ToolError:
            pass


def test_search_and_listing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, _ = _toolbox(root)
        listing = box.run("list_directory", json.dumps({"path": "."}))
        assert "app.py" in listing and "sub/" in listing
        found = box.run("search_in_project", json.dumps({"query": "greet"}))
        assert "app.py:1" in found
        by_extension = box.run("search_in_project", json.dumps({"query": "greet", "glob": ".py"}))
        assert "app.py:1" in by_extension
        regex = box.run("search_in_project", json.dumps({
            "query": r"def\s+greet", "regex": True, "glob": "*.py"}))
        assert "app.py:1" in regex


def test_terminal_command_captures_output():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, approvals = _toolbox(root)
        result = box.run("run_terminal_command",
                         json.dumps({"command": "echo agent-ok", "timeout_sec": 20}))
        assert "agent-ok" in result
        assert "Код возврата" in result
        # Commands are only available in the "full" mode, which asks nothing.
        assert approvals == []


def test_terminal_command_can_be_stopped_without_waiting_for_timeout():
    import threading
    import time
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stopped = threading.Event()
        command = subprocess.list2cmdline(
            [sys.executable, "-c", "import time; time.sleep(5)"])
        box = LocalToolbox(str(root), should_stop=stopped.is_set)
        timer = threading.Timer(0.25, stopped.set)
        started = time.monotonic()
        timer.start()
        try:
            result = box.run_terminal_command(command, timeout_sec=10)
        finally:
            timer.cancel()
        assert "остановлена пользователем" in result
        assert time.monotonic() - started < 3


def test_project_root_required():
    box = LocalToolbox(None, confirm=lambda *a: True)
    try:
        box.run("read_file", json.dumps({"path": "a.py"}))
        raise AssertionError("работа без проекта разрешена")
    except ToolError as exc:
        assert "Проект не открыт" in str(exc)


def test_definitions_are_valid_openai_tools():
    names = [item["function"]["name"] for item in TOOL_DEFINITIONS]
    assert {"read_file", "write_file", "run_terminal_command",
            "open_application", "search_in_project", "list_directory"} <= set(names)
    for item in TOOL_DEFINITIONS:
        assert item["type"] == "function"
        function = item["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"


def test_bad_arguments_are_reported():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        box, _ = _toolbox(root)
        for payload in ("not json", json.dumps({"unknown": 1})):
            try:
                box.run("read_file", payload)
            except ToolError:
                continue
            raise AssertionError(f"плохие аргументы приняты: {payload}")


def test_access_modes_enforced_at_dispatch():
    expected = {
        "read_only": {"read_file", "open_file", "list_directory", "search_in_project"},
        "write_only": {"write_file", "edit_file"},
        "full": {d["function"]["name"] for d in TOOL_DEFINITIONS},
        "invalid": {"read_file", "open_file", "list_directory", "search_in_project"},
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        original = (root / "app.py").read_text(encoding="utf-8")
        for mode, allowed in expected.items():
            box, approvals = _toolbox(root, access_mode=mode)
            if mode in ("read_only", "invalid"):
                allowed = allowed | {"editor_state", "editor_cursor", "workspace_state"}
            assert {d["function"]["name"] for d in box.definitions()} == allowed
            for definition in TOOL_DEFINITIONS:
                name = definition["function"]["name"]
                if name in allowed:
                    continue
                try:
                    box.run(name, '{}')
                except ToolError as exc:
                    assert "запрещён" in str(exc)
                else:
                    raise AssertionError(f"{mode} allowed {name}")
            assert approvals == []
        assert (root / "app.py").read_text(encoding="utf-8") == original
        box, _ = _toolbox(root, access_mode="write_only")
        box.run("write_file", json.dumps({"path": "new.txt", "content": "written"}))
        assert (root / "new.txt").read_text() == "written"


if __name__ == "__main__":
    tests = [
        ("test_access_modes_enforced_at_dispatch", test_access_modes_enforced_at_dispatch),
        ("test_read_file_returns_content", test_read_file_returns_content),
        ("test_read_file_prefers_the_open_unsaved_editor_buffer",
         test_read_file_prefers_the_open_unsaved_editor_buffer),
        ("test_open_file_routes_to_the_ide_callback", test_open_file_routes_to_the_ide_callback),
        ("test_edit_file_replaces_only_a_unique_exact_fragment",
         test_edit_file_replaces_only_a_unique_exact_fragment),
        ("test_edit_file_rejects_non_unique_fragments", test_edit_file_rejects_non_unique_fragments),
        ("test_paths_outside_project_are_rejected", test_paths_outside_project_are_rejected),
        ("test_write_file_asks_in_restricted_mode_and_writes",
         test_write_file_asks_in_restricted_mode_and_writes),
        ("test_full_access_writes_without_any_prompt",
         test_full_access_writes_without_any_prompt),
        ("test_rejected_write_leaves_file_untouched",
         test_rejected_write_leaves_file_untouched),
        ("test_permissions_can_disable_tools", test_permissions_can_disable_tools),
        ("test_search_and_listing", test_search_and_listing),
        ("test_terminal_command_captures_output", test_terminal_command_captures_output),
        ("test_project_root_required", test_project_root_required),
        ("test_definitions_are_valid_openai_tools", test_definitions_are_valid_openai_tools),
        ("test_bad_arguments_are_reported", test_bad_arguments_are_reported),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
