import base64
import json
import subprocess
import sys
from threading import Event

import pytest

from services.agent_tools import LocalToolbox, ToolError
from services.ide_mcp import IDEMCP


def test_patch_preflights_all_files_and_applies_add_update_move_delete(tmp_path):
    (tmp_path / "a.py").write_text("first\nold\nlast\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("bye", encoding="utf-8")
    box = LocalToolbox(str(tmp_path))
    patch = """*** Begin Patch
*** Add File: new/b.py
+created
*** Update File: a.py
*** Move to: moved/a.py
@@
 first
-old
+new
 last
*** Delete File: delete.txt
*** End Patch"""
    result = json.loads(IDEMCP(box).call("apply_patch", {"patch": patch}))
    assert result["ok"] and len(result["changes"]) == 3
    assert (tmp_path / "new/b.py").read_text() == "created\n"
    assert (tmp_path / "moved/a.py").read_text() == "first\nnew\nlast\n"
    assert not (tmp_path / "a.py").exists() and not (tmp_path / "delete.txt").exists()


def test_invalid_later_patch_does_not_apply_earlier_file(tmp_path):
    (tmp_path / "a").write_text("same\nsame\n")
    patch = """*** Begin Patch
*** Add File: new
+created
*** Update File: a
@@
-same
+changed
*** End Patch"""
    with pytest.raises(ToolError, match="несколько"):
        LocalToolbox(str(tmp_path)).apply_patch(patch)
    assert not (tmp_path / "new").exists()
    assert (tmp_path / "a").read_text() == "same\nsame\n"


def test_patch_preserves_unsaved_buffer_and_disk(tmp_path):
    (tmp_path / "a").write_text("disk\n")
    buffer = ["unsaved\n"]

    def ui(name, args):
        if name == "read_buffer":
            return buffer[0]
        assert name == "replace_buffer" and args["expected"] == buffer[0]
        buffer[0] = args["content"]
        return "updated"

    result = json.loads(LocalToolbox(str(tmp_path), ui_action=ui).apply_patch(
        "*** Begin Patch\n*** Update File: a\n@@\n-unsaved\n+updated\n*** End Patch"))
    assert result["changes"][0]["unsaved"]
    assert buffer == ["updated\n"] and (tmp_path / "a").read_text() == "disk\n"


def test_patch_detects_change_during_approval(tmp_path):
    path = tmp_path / "a"
    path.write_text("old\n")

    def confirm(*_):
        path.write_text("user edit\n")
        return True

    box = LocalToolbox(str(tmp_path), access_mode="write_only", confirm=confirm)
    with pytest.raises(ToolError, match="изменился"):
        box.apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch")
    assert path.read_text() == "user edit\n"


@pytest.mark.parametrize("tool,args", [
    ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: a\n+no\n*** End Patch"}),
    ("exec_command", {"cmd": "echo denied"}),
    ("write_stdin", {"session_id": "unknown"}),
])
def test_new_mutating_tools_obey_read_only_mode(tmp_path, tool, args):
    box = LocalToolbox(str(tmp_path), access_mode="read_only")
    with pytest.raises(ToolError, match="запрещён"):
        box.run(tool, json.dumps(args))
    assert list(tmp_path.iterdir()) == []


def test_find_files_is_paged_and_honors_ignores(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/b.py").write_text("")
    (tmp_path / "ignored.py").write_text("")
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    box = LocalToolbox(str(tmp_path))
    first = json.loads(box.find_files("**/*.py", limit=1))
    second = json.loads(box.find_files("**/*.py", offset=first["next_offset"], limit=1))
    assert first["files"] + second["files"] == ["a.py", "sub/b.py"]
    assert second["next_offset"] is None
    with pytest.raises(ToolError):
        box.find_files("../*.py")


def test_search_supports_case_sensitive_regex_file_and_count_modes(tmp_path):
    (tmp_path / "a.py").write_text("VALUE=1\nvalue=2\n")
    (tmp_path / "b.py").write_text("value=3\n")
    box = LocalToolbox(str(tmp_path))
    assert box.search_in_project(r"VALUE=\d", regex=True, case_sensitive=True,
                                 output_mode="files", glob="**/*.py") == "a.py"
    assert "a.py: 2" in box.search_in_project("value", output_mode="count")


def _command(script):
    return subprocess.list2cmdline([sys.executable, "-u", "-c", script])


def test_command_session_accepts_stdin_and_returns_only_new_output(tmp_path):
    box = LocalToolbox(str(tmp_path))
    try:
        first = json.loads(box.exec_command(_command("print('ready', flush=True); print('got:' + input())"),
                                           shell="cmd", yield_time_ms=100))
        assert not first["completed"] and first["session_id"]
        next_page = json.loads(box.write_stdin(first["session_id"], chars="hello\n", yield_time_ms=3000))
        combined = first["output"] + next_page["output"]
        assert combined.count("ready") == 1 and "got:hello" in combined
        assert next_page["completed"] and next_page["exit_code"] == 0
        assert not box.command_sessions.sessions
    finally:
        box.close()


def test_command_output_is_paged_without_loss(tmp_path):
    box = LocalToolbox(str(tmp_path))
    try:
        page = json.loads(box.exec_command(_command("print('x' * 2000)"), shell="cmd",
                                          yield_time_ms=3000, max_output_chars=256))
        output = page["output"]
        while page["has_more_output"] or not page["completed"]:
            page = json.loads(box.write_stdin(page["session_id"], max_output_chars=256, yield_time_ms=100))
            output += page["output"]
        assert output.strip() == "x" * 2000
    finally:
        box.close()


def test_stop_terminates_session_without_waiting_for_next_model_call(tmp_path):
    stopped = Event()
    box = LocalToolbox(str(tmp_path), should_stop=stopped.is_set)
    try:
        result = json.loads(box.exec_command(_command("import time; time.sleep(60)"), shell="cmd", yield_time_ms=0))
        stopped.set()
        session = box.command_sessions.sessions[result["session_id"]]
        assert session.finished.wait(8)
        assert session.process.poll() is not None
        assert session.reason == "cancelled"
        capture = session.path
    finally:
        box.close()
    assert not capture.exists()


def test_command_timeout_and_task_cleanup(tmp_path):
    box = LocalToolbox(str(tmp_path))
    try:
        result = json.loads(box.exec_command(_command("import time; time.sleep(60)"), shell="cmd",
                                            timeout_sec=1, yield_time_ms=3000))
        assert result["completed"] and result["reason"] == "timeout"
        result = json.loads(box.exec_command(_command("import time; time.sleep(60)"), shell="cmd", yield_time_ms=0))
        session = box.command_sessions.sessions[result["session_id"]]
        box.close()
        assert session.process.poll() is not None and not session.path.exists()
    finally:
        box.close()


def test_view_image_uses_multimodal_mcp_result_and_checks_project_bounds(tmp_path):
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9z8AAAAASUVORK5CYII=")
    (tmp_path / "image.png").write_bytes(data)
    box = LocalToolbox(str(tmp_path), access_mode="read_only")
    result = IDEMCP(box).request("tools/call", {"name": "view_image", "arguments": {"path": "image.png"}})
    assert result["content"][0]["type"] == "image"
    assert base64.b64decode(result["imageResult"]["__image__"]) == data
    with pytest.raises(ToolError, match="вне проекта"):
        box.view_image("../outside.png")


def test_patch_updates_use_team_snapshots(tmp_path):
    from services.team_cache import TeamCache

    project = tmp_path / "project"
    project.mkdir()
    (project / "a").write_text("before\n")
    box = LocalToolbox(str(project))
    box.team_cache = TeamCache(tmp_path / "cache")
    box.team_run_id = "task"
    result = json.loads(box.apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-before\n+after\n*** End Patch"))
    assert result["ok"]
    snapshots = box.team_cache.snapshots(str(project))
    assert len(snapshots) == 1
    box.team_cache.restore(str(project), snapshots[0]["id"], box)
    assert (project / "a").read_text() == "before\n"


def test_command_test_evidence_is_invalidated_by_edits_during_process():
    from services.task_evidence import TaskEvidence

    evidence = TaskEvidence("исправь и запусти тесты")
    started = {"session_id": "s", "command": "python -m pytest", "cwd": "project",
               "completed": False, "exit_code": None}
    evidence.observe("exec_command", {"cmd": "python -m pytest"}, json.dumps(started))
    evidence.observe("apply_patch", {}, json.dumps({"ok": True, "changes": [
        {"path": "a.py", "unsaved": False, "operation": "Update"},
    ]}))
    evidence.observe("write_stdin", {"session_id": "s"}, json.dumps({**started, "completed": True, "exit_code": 0}))
    assert evidence.blocking_reason("готово", []) == "Нет успешного результата запрошенных тестов для текущего состояния."
