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
from services.workbench_tools import TOOLS as WORKBENCH_TOOLS


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


def test_direct_ai_edit_does_not_replace_concurrent_changes(tmp_path):
    import pytest

    path = tmp_path / "sample.txt"
    path.write_text("old\n", encoding="utf-8")

    def confirm(*_args):
        path.write_text("external\nold\n", encoding="utf-8")
        return True

    box = LocalToolbox(str(tmp_path), access_mode="write_only", confirm=confirm)
    with pytest.raises(ToolError, match="изменён на диске"):
        box.edit_file(path.name, "old", "new")
    assert path.read_text(encoding="utf-8") == "external\nold\n"


def test_read_file_streams_a_range_and_caps_output(tmp_path, monkeypatch):
    from services import agent_tools

    monkeypatch.setattr(agent_tools, "MAX_READ_BYTES", 4)
    monkeypatch.setattr(agent_tools, "MAX_READ_CHARS", 30)
    path = tmp_path / "sample.txt"
    path.write_text("A" * 100 + "\nsecond\nthird\n", encoding="utf-8")
    box = LocalToolbox(str(tmp_path))
    assert box.read_file("sample.txt", offset=2, limit=1) == "[строки 2–2]\nsecond"
    assert "Достигнут предел" in box.read_file("sample.txt")
    assert "отсутствуют" in box.read_file("sample.txt", offset=5)


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
        box, _ = _toolbox(root, access_mode="read_only")
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
    box = LocalToolbox(None, confirm=lambda *a: True, access_mode="read_only")
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
        "read_only": {"read_file", "read_document", "open_file", "list_directory", "search_in_project", "process_monitor"},
        "write_only": {"write_file", "edit_file", "apply_patch", "process_monitor"},
        "full": {d["function"]["name"] for d in TOOL_DEFINITIONS} - WORKBENCH_TOOLS.keys() - {"capture_screen"},
        "invalid": {"read_file", "read_document", "open_file", "list_directory", "search_in_project", "process_monitor"},
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_project(root)
        original = (root / "app.py").read_text(encoding="utf-8")
        for mode, allowed in expected.items():
            box, approvals = _toolbox(root, access_mode=mode)
            if mode in ("read_only", "invalid"):
                allowed = allowed | {"editor_state", "editor_cursor", "workspace_state", "find_files", "view_image"}
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


def test_screen_capture_in_full_mode_reaches_model(tmp_path):
    from services.agent_harness import AgentHarness
    from services.ai_service import ToolCall
    from services.ide_mcp import IDEMCP

    box, approvals = _toolbox(tmp_path, access_mode="full", ui_action=lambda *_: json.dumps({
        "__image__": "ZmFrZQ==", "media_type": "image/png"}))
    image = IDEMCP(box).call("capture_screen", {})
    assert approvals == []

    class Provider:
        is_anthropic = False

    harness = AgentHarness(Provider(), tools=box.definitions(), tool_runner=lambda *_: image)
    messages, runs = [], []
    harness._handle_call(messages, ToolCall("shot", "capture_screen", {}), runs, {})
    assert messages[-1]["content"][1]["image_url"]["url"].endswith("ZmFrZQ==")
    assert "ZmFrZQ==" not in runs[0].output


def test_project_test_tool_uses_local_runner(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    box, _ = _toolbox(tmp_path, access_mode="full")
    commands = []
    box.run_terminal_command = lambda command, timeout_sec=30: commands.append(command) or "Код возврата: 0"
    assert box.run("run_project_tests", "{}") == "Код возврата: 0"
    assert "pytest" in commands[0] and " -q" in commands[0]


def test_python_pytest_and_pip_tools_build_project_commands(tmp_path):
    box, approvals = _toolbox(tmp_path, access_mode="full")
    commands = []
    box.run_terminal_command = lambda command, timeout_sec=30: commands.append(command) or "Код возврата: 0"

    assert box.run("python", json.dumps({"code": "print(40 + 2)"})) == "Код возврата: 0"
    assert "-c \"print(40 + 2)\"" in commands[-1] or "-c" in commands[-1]
    (tmp_path / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    box.run("python", json.dumps({"path": "tool.py", "args": "--fast"}))
    assert commands[-1].endswith("tool.py --fast")

    box.run("pytest", "{}")
    assert "-m pytest -q" in commands[-1]
    box.run("pytest", json.dumps({"target": "tests", "args": "-k agent"}))
    assert "-m pytest" in commands[-1] and "tests -k agent" in commands[-1]

    box.run("pip", json.dumps({"action": "install", "packages": "pytest requests"}))
    assert "-m pip install pytest requests" in commands[-1]
    box.run("pip", json.dumps({"action": "list"}))
    assert "-m pip list" in commands[-1]
    assert approvals == []


def test_python_and_pip_tools_validate_arguments(tmp_path):
    import pytest

    box, _ = _toolbox(tmp_path, access_mode="full")
    box.run_terminal_command = lambda command, timeout_sec=30: "Код возврата: 0"
    with pytest.raises(ToolError, match="либо code"):
        box.run("python", "{}")
    with pytest.raises(ToolError, match="packages"):
        box.run("pip", json.dumps({"action": "install"}))
    with pytest.raises(ToolError, match="Действие pip"):
        box.run("pip", json.dumps({"action": "publish"}))
    with pytest.raises(ToolError, match="timeout_sec"):
        box.run("pytest", json.dumps({"timeout_sec": "10"}))


def test_git_tool_runs_allowed_subcommand_only(tmp_path, monkeypatch):
    import pytest

    box, _ = _toolbox(tmp_path, access_mode="full")
    commands = []
    box.run_terminal_command = lambda command, timeout_sec=30: commands.append(command) or "Код возврата: 0"
    monkeypatch.setattr(box, "_git_executable", lambda: "git")
    assert box.run("git", json.dumps({"args": "status --short"})) == "Код возврата: 0"
    assert commands[-1].endswith("git status --short")
    with pytest.raises(ToolError, match="не поддерживается"):
        box.run("git", json.dumps({"args": "filter-branch --all"}))
    with pytest.raises(ToolError, match="аргументы git"):
        box.run("git", "{}")


def test_toolchain_tools_require_full_access(tmp_path):
    box, _ = _toolbox(tmp_path, access_mode="read_only")
    names = {item["function"]["name"] for item in box.definitions()}
    assert {"python", "git", "pytest", "pip"}.isdisjoint(names)


def test_screen_capture_full_access_skips_app_confirmation(tmp_path):
    box = LocalToolbox(str(tmp_path), access_mode="full", confirm=lambda *_: False,
                       ui_action=lambda *_: json.dumps({"__image__": "ZmFrZQ=="}))
    assert box.run("capture_screen", "{}") == {"__image__": "ZmFrZQ=="}


def test_full_access_reads_and_edits_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("before", encoding="utf-8")
    box = LocalToolbox(str(project), access_mode="full")
    assert "before" in box.run("read_file", json.dumps({"path": str(outside)}))
    assert "Изменён" in box.run("edit_file", json.dumps({
        "path": str(outside), "old_text": "before", "new_text": "after"}))
    assert outside.read_text(encoding="utf-8") == "after"
    box.run("write_file", json.dumps({"path": str(outside), "content": "final"}))
    assert outside.read_text(encoding="utf-8") == "final"


def test_full_access_runs_cmd_and_powershell_in_external_directory(tmp_path):
    import os
    import shutil
    import pytest

    if os.name != "nt" or not (shutil.which("pwsh") or shutil.which("powershell")):
        pytest.skip("Windows PowerShell is unavailable")
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    box = LocalToolbox(str(project), access_mode="full")
    cmd = box.run("run_terminal_command", json.dumps({
        "command": "echo agent-cmd", "shell": "cmd", "cwd": str(other)}))
    ps = box.run("run_terminal_command", json.dumps({
        "command": "Write-Output agent-ps", "shell": "powershell", "cwd": str(other)}))
    assert "Код возврата: 0" in cmd and "agent-cmd" in cmd
    assert "Код возврата: 0" in ps and "agent-ps" in ps


def test_workbench_permissions_and_project_paths(tmp_path):
    import pytest

    calls = []
    box, approvals = _toolbox(tmp_path, access_mode="write_only",
                              ui_action=lambda name, payload: calls.append((name, payload)))
    names = {item["function"]["name"] for item in box.definitions()}
    assert {"todo_edit", "editor_save", "explorer_edit", "project_config_edit"} <= names
    assert {"todo_list", "terminal_execute", "browser_read"}.isdisjoint(names)
    box.run("todo_edit", json.dumps({"action": "add", "title": "Check", "path": "app.py"}))
    assert approvals == ["todo_edit"]
    assert calls[0][1]["path"] == str(tmp_path / "app.py")
    assert calls[0][1]["_project_root"] == str(tmp_path)
    with pytest.raises(ToolError, match="вне проекта"):
        box.run("explorer_edit", json.dumps({"action": "move", "path": "app.py", "destination": "../outside.py"}))
    with pytest.raises(ToolError, match="Недопустимое"):
        box.run("project_config_edit", json.dumps({"analyzers": ["unknown"]}))
    full, _ = _toolbox(tmp_path, allow_commands=False, ui_action=lambda *_: "")
    with pytest.raises(ToolError, match="запрещён"):
        full.run("terminal_execute", json.dumps({"command": "echo test"}))


def test_git_agent_handles_repository_before_first_commit(tmp_path):
    import shutil
    import subprocess
    from types import SimpleNamespace

    import pytest
    from app.agent_workbench import _git_operation

    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")

    def run(args, *, root):
        completed = subprocess.run(
            [git, *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        return completed.returncode == 0, (completed.stdout or completed.stderr).strip()

    root = tmp_path.resolve()
    assert run(["init"], root=root)[0]
    panel = SimpleNamespace(_run=run)
    path = root / "new file.txt"
    path.write_text("staged", encoding="utf-8")

    assert "нет коммитов" in _git_operation(panel, root, {"action": "log"})["output"]
    _git_operation(panel, root, {"action": "stage", "path": str(path)})
    assert run(["ls-files"], root=root) == (True, path.name)
    path.write_text("working copy", encoding="utf-8")
    _git_operation(panel, root, {"action": "unstage", "path": str(path)})
    assert run(["ls-files"], root=root) == (True, "")
    assert path.read_text(encoding="utf-8") == "working copy"


def test_background_edits_do_not_open_tabs_and_preserve_open_buffers(tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("value = 1", encoding="utf-8")
    calls = []
    buffer = "__NO_OPEN_EDITOR_BUFFER__"

    def ui(kind, payload):
        calls.append((kind, payload))
        assert kind != "open_file"
        return buffer if kind == "read_buffer" else "buffer updated"

    box = LocalToolbox(str(tmp_path), ui_action=ui)
    box.background = True
    box.edit_file("demo.py", "value = 1", "value = 2")
    assert target.read_text(encoding="utf-8") == "value = 2"
    box.write_file("other.py", "created")
    assert (tmp_path / "other.py").read_text(encoding="utf-8") == "created"
    buffer = "unsaved = 1"
    assert box.edit_file("demo.py", "unsaved = 1", "unsaved = 2") == "buffer updated"
    assert calls[-1][0] == "edit_file"
    assert calls[-1][1]["expected"] == "unsaved = 1"
    assert target.read_text(encoding="utf-8") == "value = 2"


def test_ide_mcp_enforces_project_tool_permissions(tmp_path):
    from services.ide_mcp import IDEMCP

    (tmp_path / "main.py").write_text("answer = 42", encoding="utf-8")
    toolbox, _ = _toolbox(tmp_path, access_mode="read_only")
    mcp = IDEMCP(toolbox)
    listed = {tool["name"] for tool in mcp.request("tools/list")["tools"]}
    assert "read_file" in listed and "write_file" not in listed
    assert "answer = 42" in mcp.call("read_file", {"path": "main.py"})
    assert mcp.request("tools/call", {"name": "write_file"})["isError"]
    assert mcp.request("tools/call", {"name": "read_file", "arguments": {"path": "../outside"}})["isError"]


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


def test_default_read_is_bounded_and_can_continue(tmp_path):
    path = tmp_path / 'large.py'
    path.write_text(''.join(f'line {i}\n' for i in range(1, 501)))
    box = LocalToolbox(str(tmp_path))
    first = box.read_file('large.py')
    assert 'line 200\n' in first and 'line 201' not in first
    assert 'offset=201' in first
    second = box.read_file('large.py', offset=201, limit=200)
    assert 'line 201\n' in second and 'line 400' in second
    whole = box.read_file('large.py', limit=0)
    assert 'line 500' in whole and 'Достигнут предел' not in whole


def test_mcp_reads_pdf_in_pages_even_when_the_model_calls_read_file(tmp_path):
    import pymupdf

    from services.ide_mcp import IDEMCP

    path = tmp_path / "notes.pdf"
    with pymupdf.open() as pdf:
        for index in range(7):
            pdf.new_page().insert_text((24, 24), f"Page {index + 1}")
        pdf.save(path)

    local = IDEMCP(LocalToolbox(str(tmp_path), access_mode="read_only"))
    names = {tool["name"] for tool in local.list_tools()}
    assert "read_document" in names
    first = json.loads(local.call("read_file", {"path": path.name, "limit": 200}))
    assert first["unit"] == "page"
    assert [page["page"] for page in first["items"]] == [1, 2, 3, 4, 5]
    assert first["next_offset"] == 6
    later = json.loads(local.call("read_document", {"path": path.name, "offset": 6}))
    assert "Page 6" in later["items"][0]["text"] and later["next_offset"] is None

    restricted = IDEMCP(LocalToolbox(str(tmp_path), access_mode="write_only"))
    assert "Ошибка MCP CodeLane" in restricted.call("read_document", {"path": path.name})


def test_spreadsheet_read_yields_a_range_and_next_offset(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    path = tmp_path / "rows.xlsx"
    for i in range(1, 11):
        workbook.active.append([f"value-{i}"])
    workbook.save(path)
    workbook.close()

    box = LocalToolbox(str(tmp_path))
    page = json.loads(box.read_document(path.name, offset=2, limit=2))
    assert page["text"] == "1\tvalue-1\n2\tvalue-2"
    assert page["next_offset"] == 4


def test_read_document_accepts_markdown_and_reports_missing_path(tmp_path):
    import pytest

    (tmp_path / "README.md").write_text("# Report\nSecond line\n", encoding="utf-8")
    box = LocalToolbox(str(tmp_path))
    assert box.read_document("README.md", offset=2, limit=1).endswith("Second line")
    with pytest.raises(ToolError, match="Файл не найден"):
        box.read_document("missing.docx")


def test_background_terminal_execute_routes_to_the_visible_terminal(tmp_path, monkeypatch):
    import pytest

    calls = []
    box = LocalToolbox(str(tmp_path), ui_action=lambda name, args: calls.append((name, args)) or "ui started")
    box.background = True
    monkeypatch.setattr(box, "run_terminal_command",
                        lambda *_: pytest.fail("terminal_execute was redirected to a hidden process"))
    assert box.run("terminal_execute", '{"command":"echo ready","wait_seconds":0}') == "ui started"
    assert calls == [("terminal_execute", {
        "command": "echo ready", "wait_seconds": 0, "_project_root": str(tmp_path),
    })]


def test_windows_command_output_uses_the_oem_code_page(monkeypatch):
    import ctypes
    import os
    from types import SimpleNamespace

    import pytest

    from core.subprocess_utils import decode_console_output

    if os.name != "nt":
        pytest.skip("Windows console encoding")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(GetOEMCP=lambda: 866)))
    message = "Команда не найдена."
    assert decode_console_output(message.encode("cp866")) == message
    assert decode_console_output(message.encode("utf-8")) == message


def test_scratch_files_of_the_request_are_removed_on_close(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "tmp_keep.py").write_text("# файл пользователя\n", encoding="utf-8")
    box = LocalToolbox(str(tmp_path))
    box.write_file("tmp_script.py", "print('scratch')")
    box.write_file("report.md", "# отчёт")
    assert (tmp_path / "tmp_script.py").exists()

    box.close()

    assert not (tmp_path / "tmp_script.py").exists()
    assert box.removed_temp_files == ["tmp_script.py"]
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "# отчёт"
    # Существовавший до запроса черновик пользователя остаётся.
    assert (tmp_path / "tmp_keep.py").exists()
    assert (tmp_path / "app.py").exists()


def test_cleanup_covers_scratch_files_from_commands_in_subdirectories(tmp_path):
    (tmp_path / "pkg").mkdir()
    box = LocalToolbox(str(tmp_path))
    box.write_file("tmp_probe.py", "x")
    # Так могла бы оставить файл запущенная агент командой.
    (tmp_path / "pkg" / "tmp_deep.ps1").write_text("x", encoding="utf-8")

    box.close()

    assert set(box.removed_temp_files) == {"tmp_probe.py", "pkg/tmp_deep.ps1"}
    assert not (tmp_path / "pkg" / "tmp_deep.ps1").exists()
    assert (tmp_path / "pkg").is_dir()


def test_cleanup_can_be_switched_off(tmp_path):
    box = LocalToolbox(str(tmp_path), clean_temp_files=False)
    box.write_file("tmp_script.py", "x")
    box.close()
    assert (tmp_path / "tmp_script.py").exists()
    assert box.removed_temp_files == []


def test_temporary_name_matches_drafts_only():
    from services.temp_files import is_temporary_name

    for name in ("tmp_ai.py", "tmp.py", "temp_scan.ps1", "scratch.txt", "_tmp.csv", "data.tmp", "~$report.docx"):
        assert is_temporary_name(name), name
    for name in ("template.py", "temperature.py", "attempt.py", "app.py", "tmp"):
        assert not is_temporary_name(name), name
