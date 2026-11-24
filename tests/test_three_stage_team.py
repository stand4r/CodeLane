import json
from services.agent_team import run_team
from services.agent_tools import LocalToolbox


class Script:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.config = {"ai_team_strategy": "three_stage"}
        self.last_usage = {}
        self.seen = []

    def converse(self, prompt, *, tools, tool_runner, **kwargs):
        self.seen.append((prompt, [t["function"]["name"] for t in tools]))
        fn = next(self.actions)
        fn(tool_runner, tools)
        self.last_usage = {"total_tokens": 10}
        return ""


def finish(status="ready", summary="facts"):
    return {"status": status, "summary": summary, "paths": ["a.py"], "checks": ["read a.py"]}


def test_three_stages_exchange_only_task_context(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def specialist(route, tools):
        assert "edit_file" not in [t["function"]["name"] for t in tools]
        assert "недоступен" in route("write_file", {"path": "a.py", "content": "bad"})
        route("finish_stage", finish())

    def writer(route, tools):
        route("finish_stage", finish())

    def reviewer(route, tools):
        assert "Сначала" in route("finish_stage", finish("pass"))
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("pass"))

    s = Script([specialist, writer, reviewer])
    r = run_team(s, LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False)
    assert r.complete and r.usage["total_tokens"] == 30
    assert len(r.optimization["stages"]) == 3
    assert all("max_tokens" not in p for p, _ in s.seen)


def test_defect_returns_to_implementer_and_then_verifier(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def done(route, _):
        route("finish_stage", finish())

    def failed(route, _):
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("needs_changes", "defect"))

    def passed(route, _):
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("pass"))

    r = run_team(
        Script([done, done, failed, done, passed]),
        LocalToolbox(str(tmp_path)),
        "task",
        lambda *_: None,
        lambda: False,
    )
    assert r.complete and len(r.optimization["stages"]) == 5


def test_missing_handoff_is_incomplete(tmp_path):
    r = run_team(
        Script([lambda *_: None]),
        LocalToolbox(str(tmp_path)),
        "task",
        lambda *_: None,
        lambda: False,
    )
    assert not r.complete and r.failed_agents == 1


def test_repeated_defect_stops_without_false_success(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def done(route, _):
        route("finish_stage", finish())

    def failed(route, _):
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("needs_changes", "same defect"))

    r = run_team(
        Script([done, done, failed, done, failed]),
        LocalToolbox(str(tmp_path)),
        "task",
        lambda *_: None,
        lambda: False,
    )
    assert not r.complete and len(r.optimization["stages"]) == 5


def test_user_question_stops_before_implementation(tmp_path):
    def ask(route, _):
        route("ask_user", {"question": "Какое поведение нужно?"})

    result = run_team(
        Script([ask]), LocalToolbox(str(tmp_path)), "unclear", lambda *_: None, lambda: False
    )
    assert result.question == "Какое поведение нужно?" and not result.complete
    assert len(result.optimization["stages"]) == 1


def test_discovery_respects_role_permissions(tmp_path):
    def inspect(route, tools):
        result = route("discover_tools", {"query": "write_file"})
        assert json.loads(result) == []
        route("finish_stage", finish("blocked"))

    result = run_team(
        Script([inspect]), LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False
    )
    assert not result.complete


def test_verifier_cannot_edit_directly(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def done(route, _):
        route("finish_stage", finish())

    def check(route, tools):
        assert "edit_file" not in [t["function"]["name"] for t in tools]
        assert "недоступен" in route("write_file", {"path": "a.py", "content": "bad"})
        assert json.loads(route("discover_tools", {"query": "write_file"})) == []
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("pass"))

    assert run_team(
        Script([done, done, check]),
        LocalToolbox(str(tmp_path)),
        "task",
        lambda *_: None,
        lambda: False,
    ).complete
    assert (tmp_path / "a.py").read_text() == "x=1"


def test_usage_is_counted_when_stage_raises_after_successful_round(tmp_path):
    class Broken(Script):
        def converse(self, prompt, **kwargs):
            kwargs["on_usage"]({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
            raise RuntimeError("network failed")

    r = run_team(Broken([]), LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False)
    assert not r.complete and r.usage["total_tokens"] == 15


def test_specialist_does_not_mark_whole_task_as_needs_changes(tmp_path):
    def inspect(route, _):
        assert "ready" in route("finish_stage", finish("needs_changes"))
        route("finish_stage", finish("blocked", "missing file"))

    assert not run_team(
        Script([inspect]), LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False
    ).complete


def test_user_stop_does_not_start_any_model_call(tmp_path):
    s = Script([])
    assert not run_team(
        s, LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: True
    ).complete
    assert not s.seen


def test_dynamic_schemas_reach_the_next_model_request():
    from services.agent_harness import AgentHarness
    from services.ai_service import ToolCall

    class Provider:
        is_anthropic = False
        truncated = False
        last_usage = {}

        def __init__(self):
            self._discovered_tools = []
            self.round = 0

        def _openai_tool_round(self, messages, tools, *args):
            self.round += 1
            if self.round == 1:
                return "", [ToolCall("d", "discover_tools", {"query": "extra"})]
            assert any(t.get("function", {}).get("name") == "extra" for t in tools)
            return "done", []

    s = Provider()

    def route(*_):
        s._discovered_tools.append(
            {
                "type": "function",
                "function": {"name": "extra", "parameters": {"type": "object", "properties": {}}},
            }
        )
        return "registered"

    assert (
        AgentHarness(s, tools=[], tool_runner=route).run([{"role": "user", "content": "task"}]).text
        == "done"
    )


def test_implementer_pass_is_normalized_without_claiming_final_success(tmp_path):
    (tmp_path / "a.py").write_text("x=1")

    def done(route, _):
        route("finish_stage", finish())

    def implement(route, _):
        route("finish_stage", finish("pass"))

    def verify(route, _):
        route("read_file", {"path": "a.py"})
        route("finish_stage", finish("pass"))

    r = run_team(
        Script([done, implement, verify]),
        LocalToolbox(str(tmp_path)),
        "task",
        lambda *_: None,
        lambda: False,
    )
    assert r.complete and r.optimization["stages"][1]["handoff"]["status"] == "ready"


def test_shell_is_exposed_and_runs_real_python_script_in_both_working_roles(tmp_path):
    import subprocess, sys

    script = tmp_path / "probe.py"
    script.write_text("print('LIVE_PROBE_OK')")

    def specialist(route, _):
        route("finish_stage", finish())

    def worker(route, tools):
        assert "run_terminal_command" in [t["function"]["name"] for t in tools]
        assert "не подтверждён" in route("finish_stage", finish("blocked", "shell absent"))
        result = route(
            "run_terminal_command",
            {"command": subprocess.list2cmdline([sys.executable, str(script)])},
        )
        assert "Код возврата: 0" in result and "LIVE_PROBE_OK" in result
        route("finish_stage", finish("ready" if len(tools) > 0 else "blocked"))

    def verifier(route, tools):
        worker(route, tools)
        route("finish_stage", finish("pass"))

    r = run_team(
        Script([specialist, worker, verifier]),
        LocalToolbox(str(tmp_path)),
        "run script",
        lambda *_: None,
        lambda: False,
    )
    assert r.complete


def test_shell_is_not_granted_in_read_only_mode(tmp_path):
    def specialist(route, _):
        route("finish_stage", finish())

    def worker(route, tools):
        assert "run_terminal_command" not in [t["function"]["name"] for t in tools]
        assert "недоступен" in route("run_terminal_command", {"command": "python probe.py"})
        route("finish_stage", finish("blocked"))

    r = run_team(
        Script([specialist, worker]),
        LocalToolbox(str(tmp_path), access_mode="read_only"),
        "run script",
        lambda *_: None,
        lambda: False,
    )
    assert not r.complete


def test_three_stages_continue_beyond_legacy_budget(tmp_path, monkeypatch):
    from services.ai_service import AIService

    (tmp_path / "a.py").write_text("x=1")
    rounds = iter([("finish_stage", finish()), ("finish_stage", finish()),
                   ("read_file", {"path": "a.py"}), ("finish_stage", finish("pass"))])
    caps = []

    def stream(self, path, payload):
        caps.append(payload.get("max_tokens"))
        name, args = next(rounds)
        call = {"index": 0, "id": "test", "function": {"name": name, "arguments": json.dumps(args)}}
        yield {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}],
               "usage": {"prompt_tokens": 20000, "completion_tokens": 400, "total_tokens": 20400}}

    monkeypatch.setattr(AIService, "_stream_json_once", stream)
    service = AIService({"ai_provider": "ollama", "ai_team_strategy": "three_stage",
                         "ai_team_token_budget": 4_000})
    result = run_team(service, LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False)
    assert result.complete, result.answer
    assert caps == [None] * 4
    assert result.usage["prompt_tokens"] == 80_000


def test_truncated_commands_are_not_executed_or_retried(tmp_path, monkeypatch):
    from services.ai_service import AIService

    def stream(self, path, payload):
        if self._team_role == "specialist":
            name, args, reason = "finish_stage", finish(), "tool_calls"
        else:
            name, args, reason = "write_file", {"path": "partial.txt", "content": "partial"}, "length"
        call = {"id": "test", "function": {"name": name, "arguments": json.dumps(args)}}
        yield {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": reason}]}

    monkeypatch.setattr(AIService, "_stream_json_once", stream)
    service = AIService({"ai_provider": "ollama", "ai_team_strategy": "three_stage"})
    result = run_team(service, LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False)
    assert not result.complete and not (tmp_path / "partial.txt").exists()
    assert result.optimization["stage_count"] == 2


def test_large_read_reaches_model_as_preview_with_lossless_cross_stage_lookup(tmp_path, monkeypatch):
    import re
    from services.ai_service import AIService

    content = "line\n" * 1500 + "target=42\n" + "line\n" * 1500
    (tmp_path / "large.txt").write_text(content)
    evidence_id = ""
    calls = []

    def stream(self, path, payload):
        nonlocal evidence_id
        last = payload["messages"][-1]
        if self._team_role == "specialist" and last["role"] == "user":
            name, args = "read_file", {"path": "large.txt", "limit": 0}
        elif self._team_role == "specialist":
            preview = last["content"]
            assert len(preview) < len(content) // 4 and "target=42" not in preview
            evidence_id = re.search(r"id=(\w{64})", preview)[1]
            name, args = "finish_stage", finish(summary=evidence_id)
        elif self._team_role == "implementer" and last["role"] == "user":
            assert evidence_id in last["content"]
            name, args = "read_local_evidence", {"id": evidence_id, "query": "target="}
        elif self._team_role == "implementer":
            assert "target=42" in last["content"] and len(last["content"]) < 500
            name, args = "finish_stage", finish()
        elif last["role"] == "user":
            name, args = "read_file", {"path": "large.txt", "offset": 1500, "limit": 3}
        else:
            assert "target=42" in last["content"]
            name, args = "finish_stage", finish("pass")
        calls.append(name)
        call = {"id": "test", "function": {"name": name, "arguments": json.dumps(args)}}
        yield {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}],
               "usage": {"total_tokens": 500}}

    monkeypatch.setattr(AIService, "_stream_json_once", stream)
    service = AIService({"ai_provider": "ollama", "ai_team_strategy": "three_stage"})
    result = run_team(service, LocalToolbox(str(tmp_path)), "task", lambda *_: None, lambda: False)
    assert result.complete, result.answer
    assert len(calls) == 6
