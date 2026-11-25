import json

import pytest

from services.agent_context import AgentContext
from services.agent_harness import AgentHarness
from services.agent_tools import LocalToolbox
from services.ai_service import ToolCall
from services.ide_mcp import IDEMCP
from services.local_evidence import LocalEvidence


class Provider:
    is_anthropic = False
    truncated = False
    last_usage = {}

    def __init__(self, rounds):
        self.rounds = iter(rounds)
        self.seen = []

    def _openai_tool_round(self, messages, tools, *_):
        self.seen.append(messages)
        return next(self.rounds)

    def _anthropic_tool_round(self, *args):
        text, calls = self._openai_tool_round(*args)
        blocks = [{"type": "text", "text": text}] if text else []
        blocks.extend({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls)
        return blocks, calls


def _call(id, name, **args):
    return "", [ToolCall(id, name, args)]


@pytest.mark.parametrize("anthropic", [False, True])
def test_manual_compaction_keeps_plan_results_and_lossless_archive(anthropic):
    plan = [{"step": "Fix bug", "status": "in_progress"}, {"step": "Verify", "status": "pending"}]
    full = "begin\n" + "x" * 15000 + "UNIQUE_MIDDLE" + "x" * 15000 + "\nend"
    provider = Provider([
        _call("plan", "update_plan", plan=plan),
        _call("read", "read_file", path="a.py"),
        _call("edit", "edit_file", path="a.py", old_text="before", new_text="after"),
        _call("test", "run_project_tests"),
        _call("compact", "compact_context", summary="Fix applied, tests failed; investigate assertion."),
        ("done", []),
    ])
    provider.is_anthropic = anthropic
    outputs = {"read_file": full, "edit_file": "Изменён файл a.py.", "run_project_tests": "Код возврата: 1\nFAILED assertion"}
    harness = AgentHarness(provider, tools=[{}], tool_runner=lambda name, _: outputs[name])
    user = {"role": "user", "content": "Fix the bug without removing validation."}
    result = harness.run([user])
    request = provider.seen[-1]
    assert request[0] == user
    note = next(m["content"] for m in request if isinstance(m.get("content"), str)
                and m["content"].startswith("Контекст предыдущих шагов"))
    state = json.loads(note.split("\n", 1)[1])
    assert state["plan"] == plan
    assert "tests failed" in state["notes"]
    assert "Код возврата: 1" in state["commands"][-1]["result"]
    archive = json.loads(harness.evidence.read(state["archive_id"]))
    assert "UNIQUE_MIDDLE" in json.dumps(archive)
    assert any(value == full for value in harness.evidence.values.values())
    # The retained exchange has the assistant call and matching tool result.
    if anthropic:
        assert request[-2]["content"][0]["id"] == "compact"
        assert request[-1]["content"][0]["tool_use_id"] == "compact"
    else:
        assert request[-2]["tool_calls"][0]["id"] == request[-1]["tool_call_id"] == "compact"
    assert result.text == "done"


def test_invalid_plan_does_not_replace_working_plan():
    context, evidence = AgentContext(), LocalEvidence()
    plan = [{"step": "one", "status": "in_progress"}]
    context.call("update_plan", {"plan": plan}, evidence)
    with pytest.raises(ValueError):
        context.call("update_plan", {"plan": plan + [{"step": "two", "status": "in_progress"}]}, evidence)
    assert context.plan == plan


def test_live_command_polling_is_not_blocked_by_repeat_or_stall_guard():
    provider = Provider([_call(str(i), "write_stdin", session_id="session") for i in range(10)] + [("done", [])])
    calls = []

    def runner(*args):
        calls.append(args)
        return json.dumps({"session_id": "session", "completed": False, "output": "", "exit_code": None})

    result = AgentHarness(provider, tools=[{}], tool_runner=runner).run([{"role": "user", "content": "wait"}])
    assert len(calls) == 10 and result.stop_reason == "answer"


def test_external_disk_changes_invalidate_read_without_rereading_unchanged_file(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("one\n")
    box = LocalToolbox(str(tmp_path))
    provider = Provider([_call(str(i), "read_file", path="a.py") for i in range(3)] + [("done", [])])
    calls = []

    def observed(run):
        if run.repeated:
            path.write_text("new longer content\n")

    def runner(name, args):
        calls.append(name)
        return IDEMCP(box).call(name, args)

    result = AgentHarness(provider, tools=box.definitions(), tool_runner=runner,
                          read_version=box.read_version, on_tool_end=observed).run([{"role": "user", "content": "read"}])
    assert len(calls) == 2 and result.tool_runs[1].repeated
    assert "new longer content" in result.tool_runs[-1].output


def test_editor_revision_invalidates_read_without_fetching_buffer_again(tmp_path):
    (tmp_path / "a.py").write_text("disk\n")
    revision, reads = [1], []

    def ui(name, args):
        if name == "buffer_revision":
            return {"document": 42, "revision": revision[0]}
        assert name == "read_buffer"
        reads.append(name)
        return f"buffer {revision[0]}\n"

    box = LocalToolbox(str(tmp_path), ui_action=ui)
    provider = Provider([_call(str(i), "read_file", path="a.py") for i in range(3)] + [("done", [])])

    def observed(run):
        if run.repeated:
            revision[0] = 2

    result = AgentHarness(provider, tools=box.definitions(), tool_runner=IDEMCP(box).call,
                          read_version=box.read_version, on_tool_end=observed).run([{"role": "user", "content": "read"}])
    assert len(reads) == 2
    assert "buffer 2" in result.tool_runs[-1].output


def test_end_to_end_find_regex_read_patch_verify_and_compact(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    box = LocalToolbox(str(tmp_path))
    provider = Provider([
        _call("plan", "update_plan", plan=[{"step": "Update value", "status": "in_progress"}]),
        _call("find", "find_files", pattern="**/*.py"),
        _call("search", "search_in_project", query=r"value\s*=", regex=True),
        _call("read", "read_file", path="app.py"),
        _call("again", "read_file", path="app.py", offset=1, limit=200),
        _call("patch", "apply_patch", patch="*** Begin Patch\n*** Update File: app.py\n@@\n-value = 1\n+value = 2\n*** End Patch"),
        _call("check", "read_file", path="app.py"),
        _call("done-plan", "update_plan", plan=[{"step": "Update value", "status": "completed"}]),
        _call("compact", "compact_context", summary="Value updated and read back as 2."),
        ("Changed value to 2.", []),
    ])
    result = AgentHarness(provider, tools=box.definitions(), tool_runner=IDEMCP(box).call,
                          read_version=box.read_version).run([{"role": "user", "content": "Set value to 2"}])
    assert (tmp_path / "app.py").read_text() == "value = 2\n"
    assert all(run.ok for run in result.tool_runs)
    assert result.tool_runs[4].repeated
    assert result.stop_reason == "answer"
