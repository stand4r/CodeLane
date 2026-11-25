"""Tests for the agent harness: the tool loop, retries and limits.

The old agent gave up after ``MAX_TOOL_ROUNDS = 6`` rounds and cut every tool
result at 8000 characters — reading a few files exhausted the budget and the
answer stopped half-way. These tests pin the new behaviour.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ai_service import AIError, AIMessage, AIService, ToolCall  # noqa: E402
from services.agent_harness import (DISCOVERY_PREVIEW_CHARS, SAME_CALL_LIMIT,  # noqa: E402
                                    STALLED_ROUND_LIMIT, AgentHarness, AgentResult)


class ScriptedService:
    """Minimal stand-in for AIService used by the harness."""

    is_anthropic = False
    truncated = False

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.last_usage = {}
        self.requests = 0
        self.seen = []

    def _openai_tool_round(self, messages, tools, on_text, on_reasoning=None):
        self.requests += 1
        self.seen.append([dict(message) for message in messages])
        if not self.rounds:
            return "конец", []
        text, calls = self.rounds.pop(0)
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if text and on_text:
            on_text(text)
        return text, calls

    def _continue_prompt(self):
        return "продолжи"

    def _stream_until_complete(self, messages, on_text=None, on_reasoning=None, **kwargs):
        return ""


def _user_messages():
    return [{"role": "user", "content": "сделай задачу"}]


def test_legacy_round_budget_is_ignored_but_repeat_guard_still_works():
    from services.agent_harness import RoundBudget

    limit = SAME_CALL_LIMIT + 1
    budget = RoundBudget(limit)
    calls = [("", [ToolCall(str(i), "read_file", {"path": "repo.py"})]) for i in range(limit)]
    service = ScriptedService(calls)
    result = AgentHarness(service, tools=[{}], tool_runner=lambda *_: "file",
                          unlimited_rounds=True, round_budget=budget).run(_user_messages())
    assert result.stop_reason == "answer" and result.rounds == limit + 1
    assert result.tool_runs[-1].repeated
    second = ScriptedService([("unexpected", [])])
    assert AgentHarness(second, round_budget=budget).run(_user_messages()).stop_reason == "answer"
    assert second.requests == 1


def test_harness_runs_far_beyond_the_old_round_limit():
    rounds = [("", [ToolCall(f"call_{i}", "read_file", {"path": f"f{i}.py"})])
              for i in range(10)]
    rounds.append(("Готово", []))
    service = ScriptedService(rounds)
    executed = []

    def runner(name, args):
        executed.append(args["path"])
        return "x = 1\n"

    result = AgentHarness(service, tools=[{"type": "function"}], tool_runner=runner).run(
        _user_messages())
    assert result.text.endswith("Готово")
    assert result.rounds == 11, result.rounds
    assert len(executed) == 10, executed
    assert result.stop_reason == "answer"


def test_tool_results_are_passed_back_whole():
    payload = "A" * 25_000
    service = ScriptedService([
        ("", [ToolCall("c1", "read_file", {"path": "big.py"})]),
        ("Готово", []),
    ])
    harness = AgentHarness(service, tools=[{}], tool_runner=lambda name, args: payload)
    result = harness.run(_user_messages())
    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert tool_messages, "tool result must be appended to the conversation"
    assert len(tool_messages[0]["content"]) == len(payload), len(tool_messages[0]["content"])


def test_old_discovery_output_is_compacted_only_in_provider_requests():
    large = "начало\n" + "x" * 900_000 + "\nконец"
    for anthropic in (False, True):
        calls = [
            ("", [ToolCall("read", "read_file", {"path": "large.py"})]),
            ("", [ToolCall("edit", "edit_file", {})]),
            ("", [ToolCall("check", "run_command", {})]),
            ("Готово", []),
        ]
        service = ScriptedService(calls)
        service.is_anthropic = anthropic

        def anthropic_round(*args):
            _, tool_calls = service._openai_tool_round(*args)
            return [], tool_calls

        service._anthropic_tool_round = anthropic_round
        result = AgentHarness(service, tools=[{}], tool_runner=lambda *_: large).run(_user_messages())

        def tool_output(message):
            content = message["content"]
            if anthropic:
                assert content[0]["tool_use_id"] == "read"
                return content[0]["content"]
            assert message["tool_call_id"] == "read"
            return content

        assert len(tool_output(service.seen[1][2])) < 49_000
        preview = tool_output(service.seen[-1][2])
        assert preview.startswith("начало") and preview.endswith("конец")
        assert len(preview) <= DISCOVERY_PREVIEW_CHARS + 1_000
        assert tool_output(result.messages[2]) == large
        edit = service.seen[-1][4]["content"]
        edit_preview = edit[0]["content"] if anthropic else edit
        assert (len(edit_preview) <= DISCOVERY_PREVIEW_CHARS + 1_000
                and "read_local_evidence" in edit_preview)


def test_request_keeps_recent_turn_and_limits_automatic_context():
    service = AIService({})
    history = [AIMessage("user", "Старый вопрос"),
               AIMessage("assistant", "x" * 40_000),
               AIMessage("user", "Последний вопрос"),
               AIMessage("assistant", "Последний ответ")]
    prompt = "Новый запрос"
    context = "начало" + "y" * 60_000 + "конец"
    messages = service.build_messages(prompt, history, context)
    assert messages[1:3] == [item.as_dict() for item in history[-2:]]
    assert len(messages) == 4
    attached = messages[-1]["content"]
    assert attached.startswith(prompt + "\n\nначало") and attached.endswith("конец")
    assert "Контекст сокращён" in attached and len(attached) < len(context)

    follow_up = service.build_messages(prompt, history[:2])
    assert follow_up[1] == history[0].as_dict()
    assert "Сокращена история" in follow_up[2]["content"]
    assert len(history[1].content) == 40_000


def test_repeated_identical_calls_do_not_loop_forever():
    repeats = STALLED_ROUND_LIMIT + 2
    rounds = [("", [ToolCall("c", "read_file", {"path": "same.py"})])
              for _ in range(repeats)]
    rounds.append(("Хватит", []))
    service = ScriptedService(rounds)
    executed = []

    def runner(name, args):
        executed.append(name)
        return "файл прочитан"

    result = AgentHarness(service, tools=[{}], tool_runner=runner).run(_user_messages())
    assert len(executed) == 1, executed
    joined = "\n".join(str(message.get("content")) for message in result.messages)
    assert "повторно" in joined.lower() or "повтор" in joined.lower()
    assert result.stop_reason == "stalled"
    assert service.requests < repeats + 1


def test_rejected_internal_calls_stop_even_when_the_arguments_change():
    from services.agent_harness import STALLED_ROUND_LIMIT

    read = ("", [ToolCall("read", "read_file", {"path": "app.js"})])
    repeated = [
        ("", [ToolCall(str(i), "call_tool", {"name": "unknown", "arguments": {"attempt": i}})])
        for i in range(30)
    ]
    service = ScriptedService([read, *repeated])
    events = []
    result = AgentHarness(service, tools=[{}], tool_runner=lambda *_: "const enabled = true;",
                          on_tool_end=events.append).run(_user_messages())
    assert result.stop_reason == "stalled"
    assert service.requests == 1 + STALLED_ROUND_LIMIT
    assert len(events) == service.requests
    assert all(not event.ok for event in events[1:])


def test_narration_before_tools_is_not_part_of_the_answer():
    """«Читаю файл…» before a tool call must not leak into the final answer."""
    service = ScriptedService([
        ("Читаю файл и проверяю.", [ToolCall("c1", "read_file", {"path": "a.py"})]),
        ("Файл исправлен. Проверка: pytest.", []),
    ])
    result = AgentHarness(service, tools=[{}], tool_runner=lambda n, a: "x").run(
        _user_messages())
    assert result.text == "Файл исправлен. Проверка: pytest.", result.text
    assert "Читаю файл" not in result.text


def test_usage_is_summed_across_rounds():
    rounds = [("", [ToolCall("c", "read_file", {"path": f"{i}.py"})]) for i in range(4)]
    rounds.append(("ok", []))
    result = AgentHarness(ScriptedService(rounds), tools=[{}],
                          tool_runner=lambda name, args: "y").run(_user_messages())
    # 4 tool rounds + the final answer round: every request reports 10/5.
    assert result.usage.get("prompt_tokens") == 50, result.usage
    assert result.usage.get("completion_tokens") == 25, result.usage


def test_usage_includes_automatic_continuation():
    class TruncatingService(AIService):
        def _openai_tool_round(self, messages, tools, on_text, on_reasoning=None):
            self.last_finish_reason = "length"
            self.last_usage = {"total_tokens": 10}
            return "Первая часть", []

        def _stream_with(self, messages, on_reasoning=None):
            self.last_finish_reason = "stop"
            self.last_usage = {"total_tokens": 15}
            yield "и продолжение."

    result = AgentHarness(TruncatingService({})).run(_user_messages())
    assert result.text == "Первая часть и продолжение."
    assert result.usage["total_tokens"] == 25
    assert result.stop_reason == "answer"


def test_stop_request_ends_the_run_before_new_tools():
    service = ScriptedService([
        ("", [ToolCall("c1", "read_file", {"path": "a.py"})]),
        ("не должно случиться", []),
    ])
    executed = []
    harness = AgentHarness(service, tools=[{}],
                           tool_runner=lambda name, args: executed.append(name) or "x",
                           should_stop=lambda: True)
    result = harness.run(_user_messages())
    assert result.stop_reason == "stopped"
    assert executed == []


def test_transient_error_is_retried():
    from http.client import RemoteDisconnected

    class FlakyService(ScriptedService):
        def _openai_tool_round(self, messages, tools, on_text, on_reasoning=None):
            self.requests += 1
            if self.requests == 1:
                raise AIError("Слишком много запросов (429)", status=429)
            if self.requests == 2:
                raise RemoteDisconnected("Remote end closed connection without response")
            return "после повтора", []

    class FastHarness(AgentHarness):
        def _sleep(self, seconds):
            return None

    service = FlakyService([])
    result = FastHarness(service).run(_user_messages())
    assert result.text == "после повтора"
    assert service.requests == 3


def test_non_retryable_error_propagates():
    class Broken(ScriptedService):
        def _openai_tool_round(self, messages, tools, on_text, on_reasoning=None):
            raise AIError("Неверный ключ (401)", status=401)

    try:
        AgentHarness(Broken([])).run(_user_messages())
    except AIError as exc:
        assert getattr(exc, "status", None) == 401
    else:
        raise AssertionError("AIError was expected")


# ----------------------------------------------------------------------
# Request payload: no hard token cap, reasoning switch
# ----------------------------------------------------------------------
def _service(**extra):
    config = {"ai_enabled": True, "ai_provider": "deepseek",
              "ai_base_url": "https://api.deepseek.com",
              "ai_model": "deepseek-flash",
              "ai_api_key": "sk-" + "a" * 32}
    config.update(extra)
    return AIService(config=config)


def test_no_max_tokens_by_default():
    payload = _service()._payload([{"role": "user", "content": "hi"}], stream=True)
    assert "max_tokens" not in payload
    assert "reasoning_effort" not in payload


def test_explicit_budget_is_sent_only_when_larger_than_legacy_default():
    assert "max_tokens" not in _service(ai_max_tokens=8192)._payload(
        [{"role": "user", "content": "hi"}], stream=True)
    assert _service(ai_max_tokens=20000)._payload(
        [{"role": "user", "content": "hi"}], stream=True)["max_tokens"] == 20000


def test_reasoning_effort_mapping():
    payload = _service(ai_reasoning_effort="high")._payload(
        [{"role": "user", "content": "hi"}], stream=True)
    assert payload["reasoning_effort"] == "high"
    assert "reasoning_effort" not in _service(ai_reasoning_effort="none")._payload(
        [{"role": "user", "content": "hi"}], stream=True)


def test_anthropic_reasoning_and_prompt_cache():
    service = AIService(config={"ai_enabled": True, "ai_provider": "anthropic",
                                "ai_base_url": "https://api.anthropic.com/v1",
                                "ai_model": "claude-sonnet-4-5",
                                "ai_api_key": "sk-ant-" + "b" * 32,
                                "ai_reasoning_effort": "medium"})
    payload = service._payload([{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hi"}], stream=True)
    assert payload["max_tokens"] >= 16000
    assert payload["thinking"]["type"] == "enabled"
    assert payload["thinking"]["budget_tokens"] == 8192
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_rejected_reasoning_parameter_is_stripped_and_retried():
    service = _service(ai_reasoning_effort="high")
    payloads = []

    def fake_post(path, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise AIError("unknown field: reasoning_effort", status=400)
        return {"choices": [{"message": {"content": "OK"}}]}

    service._post_json_once = fake_post
    answer = service.complete("привет")
    assert answer == "OK"
    assert "reasoning_effort" in payloads[0]
    assert "reasoning_effort" not in payloads[1]
    assert service._reasoning_unsupported is True


# ----------------------------------------------------------------------
# Context: no truncation, caching
# ----------------------------------------------------------------------
def test_context_block_is_not_truncated():
    code = "x = 1\n" * 40_000          # ~240k characters
    block = AIService.build_context_block(file_path="a.py", code=code)
    assert len(block) > 200_000
    assert "файл обрезан" not in block


def test_context_cache_reuses_unchanged_files():
    from services.ai_context import ContextCache
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "app.py"
        path.write_text("print(1)\n", encoding="utf-8")
        cache = ContextCache()
        first = cache.read(path)
        second = cache.read(path)
        assert first == second == "print(1)\n"
        assert cache.stats()["hits"] == 1
        assert cache.stats()["misses"] == 1
        time.sleep(0.01)
        path.write_text("print(2)\n", encoding="utf-8")
        assert cache.read(path) == "print(2)\n"
        assert cache.stats()["misses"] == 2


# ----------------------------------------------------------------------
# Project tools: no budget on reads, pagination
# ----------------------------------------------------------------------
def test_read_file_has_no_character_budget_and_supports_paging():
    from services.agent_tools import LocalToolbox
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = "\n".join(f"line {i}" for i in range(1, 501))
        (root / "big.py").write_text(body, encoding="utf-8")
        box = LocalToolbox(str(root))
        whole = box.read_file("big.py", limit=0)
        assert whole == body, "read_file must not cut the file"
        page = box.read_file("big.py", offset=10, limit=5)
        assert "строки 10–14" in page
        assert page.splitlines()[1] == "line 10"


def test_request_references_duplicate_observations_but_preserves_changes():
    from services.local_evidence import LocalEvidence

    outputs = ["file version one", "file version one", "file version two"]
    messages = [{"role": "user", "content": "Inspect file"}]
    results = []
    for index, output in enumerate(outputs):
        call_id = f"read-{index}"
        messages.append({"role": "assistant", "tool_calls": [{"id": call_id,
                         "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        results.append((len(messages), index + 1))
        messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
    request = AgentHarness._request_messages(messages, results, 3, evidence=LocalEvidence())
    assert request[2]["content"] == "file version one"
    assert "read-0" in request[4]["content"]
    assert "file version one" not in request[4]["content"]
    assert request[6]["content"] == "file version two"
    assert messages[4]["content"] == "file version one"


TESTS = [
    test_request_references_duplicate_observations_but_preserves_changes,
    test_harness_runs_far_beyond_the_old_round_limit,
    test_tool_results_are_passed_back_whole,
    test_old_discovery_output_is_compacted_only_in_provider_requests,
    test_request_keeps_recent_turn_and_limits_automatic_context,
    test_narration_before_tools_is_not_part_of_the_answer,
    test_repeated_identical_calls_do_not_loop_forever,
    test_usage_is_summed_across_rounds,
    test_usage_includes_automatic_continuation,
    test_stop_request_ends_the_run_before_new_tools,
    test_transient_error_is_retried,
    test_non_retryable_error_propagates,
    test_no_max_tokens_by_default,
    test_explicit_budget_is_sent_only_when_larger_than_legacy_default,
    test_reasoning_effort_mapping,
    test_anthropic_reasoning_and_prompt_cache,
    test_rejected_reasoning_parameter_is_stripped_and_retried,
    test_context_block_is_not_truncated,
    test_context_cache_reuses_unchanged_files,
    test_read_file_has_no_character_budget_and_supports_paging,
]


def main():
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

def test_repeated_read_reuses_evidence_without_executing_the_tool():
    executed = []
    service = ScriptedService([
        ('', [ToolCall(str(i), 'read_file', {'path': 'a.py'})]) for i in range(3)
    ] + [('done', [])])
    harness = AgentHarness(service, tools=[{}],
                           tool_runner=lambda *args: executed.append(args) or 'first' * 1000)
    result = harness.run(_user_messages())
    assert len(executed) == 1
    assert result.tool_runs[0].output == 'first' * 1000
    assert 'Повторное чтение не выполнено' in result.tool_runs[1].output
    assert len(result.tool_runs[1].output) < 500
    assert result.tool_runs[2].repeated
    evidence_id = next(iter(harness._file_reads.values()))[1]
    assert 'first' * 1000 in harness.evidence.read(evidence_id)


def test_edit_allows_fresh_read_after_repeat_limit():
    calls = [('', [ToolCall(str(i), 'read_file', {'path': 'a.py'})])
             for i in range(SAME_CALL_LIMIT)]
    calls += [('', [ToolCall('edit', 'edit_file', {'path': 'a.py'})]),
              ('', [ToolCall('after', 'read_file', {'path': 'a.py'})]), ('done', [])]
    executed = []
    def runner(name, args):
        executed.append(name)
        return 'output'
    result = AgentHarness(ScriptedService(calls), tools=[{}], tool_runner=runner).run(_user_messages())
    assert executed.count('read_file') == 2
    assert not result.tool_runs[-1].repeated


@pytest.mark.parametrize("name", ["read_file", "read_document"])
@pytest.mark.parametrize("anthropic", [False, True])
def test_duplicate_read_in_one_batch_preserves_tool_pairing(name, anthropic):
    class Provider(ScriptedService):
        is_anthropic = anthropic

        def _anthropic_tool_round(self, *args):
            text, calls = self._openai_tool_round(*args)
            return [{"type": "text", "text": text}], calls

    calls = [ToolCall(str(i), name, {"path": "a.py"}) for i in range(2)]
    service = Provider([("", calls), ("done", [])])
    executed, started, ended = [], [], []
    result = AgentHarness(service, tools=[{}],
                          tool_runner=lambda *args: executed.append(args) or "content",
                          on_tool_start=lambda *args: started.append(args),
                          on_tool_end=ended.append).run(_user_messages())
    assert len(executed) == len(started) == 1
    assert len(ended) == 2 and ended[-1].repeated and ended[-1].ok
    for message, call in zip(result.messages[2:], calls):
        if anthropic:
            assert message["content"][0]["tool_use_id"] == call.id
        else:
            assert message["tool_call_id"] == call.id


def test_read_defaults_are_deduplicated_but_new_ranges_are_read():
    arguments = [
        {"path": "a.py"},
        {"path": "a.py", "offset": 1, "limit": 200, "column": 0},
        {"path": "a.py", "offset": 201, "limit": 200},
        {"path": "a.py", "offset": 201, "limit": 200, "column": 12000},
        {"path": "b.py"},
    ]
    service = ScriptedService([("", [ToolCall(str(i), "read_file", args)])
                               for i, args in enumerate(arguments)] + [("done", [])])
    executed = []
    result = AgentHarness(service, tools=[{}],
                          tool_runner=lambda _, args: executed.append(args) or str(args)).run(_user_messages())
    assert executed == [arguments[i] for i in (0, 2, 3, 4)]
    assert result.tool_runs[1].repeated


@pytest.mark.parametrize("action", ["edit_file", "editor_save", "run_terminal_command",
                                    "run_project_tests", "accept_proposal"])
def test_actions_invalidate_read_cache_even_on_failure(action):
    read = ToolCall("before", "read_file", {"path": "a.py"})
    service = ScriptedService([("", [read, ToolCall("action", action, {}),
                                    ToolCall("after", "read_file", read.arguments)]), ("done", [])])
    reads = []

    def runner(name, args):
        if name == "read_file":
            reads.append(name)
            return f"version {len(reads)}"
        raise RuntimeError("Action failed after modifying the file")

    result = AgentHarness(service, tools=[{}], tool_runner=runner).run(_user_messages())
    assert len(reads) == 2
    assert result.tool_runs[-1].output == "version 2"


def test_read_errors_are_retried_and_cache_is_reset_for_new_request():
    read = ToolCall("read", "read_file", {"path": "a.py"})
    service = ScriptedService([("", [read, read]), ("done", []), ("", [read]), ("done", [])])
    outputs = iter(["Ошибка MCP CodeLane: temporarily unavailable", "first", "updated"])
    harness = AgentHarness(service, tools=[{}], tool_runner=lambda *_: next(outputs))
    first = harness.run(_user_messages())
    assert not first.tool_runs[0].ok
    assert first.tool_runs[1].output == "first"
    second = harness.run(_user_messages())
    assert second.tool_runs[0].output == "updated"


def test_invalid_read_arguments_are_reported_as_tool_errors():
    from services.agent_tools import LocalToolbox

    service = ScriptedService([("", [ToolCall("bad", "read_file", {
        "path": __file__, "offset": "bad",
    })]), ("done", [])])
    box = LocalToolbox(str(ROOT))
    result = AgentHarness(service, tools=box.definitions(),
                          tool_runner=lambda name, args: getattr(box, name)(**args)).run(_user_messages())
    assert not result.tool_runs[0].ok
    assert "Ошибка инструмента" in result.tool_runs[0].output


def test_long_tool_task_compacts_observations_without_a_request_or_token_limit():
    from services import ai_budget

    tools = [{"type": "function", "function": {"name": "read_file"}}]

    class Provider(ScriptedService):
        def _openai_tool_round(self, messages, listed_tools, *_):
            self.seen.append(messages)
            assert listed_tools[:1] == tools
            assert ai_budget.request_size(messages, listed_tools) <= ai_budget.CONTEXT_COMPACTION_BYTES
            self.last_usage = {"prompt_tokens": 10_000, "completion_tokens": 500}
            index = len(self.seen)
            if index >= 23:
                return "done", []
            return "", [ToolCall(str(index), "read_file", {"path": f"file{index}.py"})]

    service = Provider([])
    result = AgentHarness(service, tools=tools, tool_runner=lambda *_: "я" * 40_000).run(_user_messages())
    assert result.stop_reason == "answer" and result.text == "done"
    assert result.rounds == 23 and result.usage["prompt_tokens"] == 230_000
    assert all(request[0] == _user_messages()[0] for request in service.seen)
    assert len(service.seen[-1][2]["content"]) <= DISCOVERY_PREVIEW_CHARS + 1_000


def test_long_explicit_user_prompt_and_provider_default_output_are_preserved(monkeypatch):
    service = AIService({"ai_provider": "ollama"})
    prompt = "part " * 25_000

    def stream(path, payload):
        assert payload["messages"][-1]["content"] == prompt
        assert "max_tokens" not in payload
        yield {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(service, "_stream_json_once", stream)
    assert service.converse(prompt) == "Done"


def test_automatic_continuation_uses_the_provider_limits(monkeypatch):
    service = AIService({"ai_provider": "ollama"})
    requests = []

    def stream(path, payload):
        requests.append(payload)
        reason = "stop" if len(requests) == 4 else "length"
        yield {"choices": [{"delta": {"content": f"Part {len(requests)}."}, "finish_reason": reason}]}

    monkeypatch.setattr(service, "_stream_json_once", stream)
    answer = service.converse("question")
    assert len(requests) == 4 and all(f"Part {part}." in answer for part in range(1, 5))
    assert service.last_stop_reason == "answer"


def test_thinking_mode_reasoning_is_passed_back_with_tool_calls():
    """Провайдеры thinking-режима требуют reasoning_content в ответе с tool_calls."""

    class ThinkingService(ScriptedService):
        last_reasoning = ""

        def _openai_tool_round(self, messages, tools, on_text, on_reasoning=None):
            text, calls = super()._openai_tool_round(messages, tools, on_text, on_reasoning)
            self.last_reasoning = "сначала прочитаю файл"
            return text, calls

    service = ThinkingService([
        ("", [ToolCall("call_1", "read_file", {"path": "a.py"})]),
        ("готово", []),
    ])

    result = AgentHarness(service, tools=[{"type": "function"}],
                          tool_runner=lambda *args: "print(1)").run(_user_messages())

    assert result.text == "готово"
    assistant_turns = [m for m in service.seen[1] if m.get("role") == "assistant"]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["tool_calls"][0]["id"] == "call_1"
    assert assistant_turns[0]["reasoning_content"] == "сначала прочитаю файл"


def test_reasoning_content_is_not_sent_without_thinking_mode():
    service = ScriptedService([
        ("", [ToolCall("call_1", "read_file", {"path": "a.py"})]),
        ("готово", []),
    ])

    AgentHarness(service, tools=[{"type": "function"}],
                 tool_runner=lambda *args: "print(1)").run(_user_messages())

    assistant_turns = [m for m in service.seen[1] if m.get("role") == "assistant"]
    assert assistant_turns and "reasoning_content" not in assistant_turns[0]


def test_thinking_by_default_provider_gets_an_empty_reasoning_field():
    """DeepSeek thinks by default and rejects a tool turn without the field.

    Even when the round produced no visible reasoning, the key must be there
    (as an empty string) or the API answers 400 "The reasoning_content in the
    thinking mode must be passed back to the API".
    """

    class ThinkingByDefault(ScriptedService):
        reasoning_required = True
        last_reasoning = ""

    service = ThinkingByDefault([
        ("", [ToolCall("call_1", "read_file", {"path": "a.py"})]),
        ("готово", []),
    ])

    AgentHarness(service, tools=[{"type": "function"}],
                 tool_runner=lambda *args: "print(1)").run(_user_messages())

    assistant_turns = [m for m in service.seen[1] if m.get("role") == "assistant"]
    assert assistant_turns and assistant_turns[0]["reasoning_content"] == ""


def _read_file_tool():
    return {"type": "function", "function": {
        "name": "read_file", "description": "Read a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}


def test_anthropic_tool_round_keeps_thinking_blocks_in_order():
    """Claude requires its signed thinking blocks back with the tool call."""
    service = AIService({"ai_provider": "anthropic", "ai_model": "claude-opus-5.5",
                         "ai_api_key": "sk-ant-test",
                         "ai_base_url": "https://api.anthropic.com/v1"})

    def stream(_path, _payload):
        yield {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}
        yield {"type": "content_block_start", "index": 0,
               "content_block": {"type": "thinking", "thinking": ""}}
        yield {"type": "content_block_delta", "index": 0,
               "delta": {"type": "thinking_delta", "thinking": "сначала прочитаю"}}
        yield {"type": "content_block_delta", "index": 0,
               "delta": {"type": "signature_delta", "signature": "sig-abc"}}
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "content_block_start", "index": 1,
               "content_block": {"type": "text", "text": ""}}
        yield {"type": "content_block_delta", "index": 1,
               "delta": {"type": "text_delta", "text": "Читаю файл."}}
        yield {"type": "content_block_stop", "index": 1}
        yield {"type": "content_block_start", "index": 2,
               "content_block": {"type": "tool_use", "id": "toolu_1",
                                 "name": "read_file", "input": {}}}
        yield {"type": "content_block_delta", "index": 2,
               "delta": {"type": "input_json_delta",
                         "partial_json": '{"path": "a.py"}'}}
        yield {"type": "content_block_stop", "index": 2}

    service._stream_json = stream
    seen = []
    blocks, calls = service._anthropic_tool_round(
        [{"role": "user", "content": "прочитай"}], [_read_file_tool()],
        seen.append, None)

    assert [block["type"] for block in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[0]["thinking"] == "сначала прочитаю"
    assert blocks[0]["signature"] == "sig-abc"
    assert blocks[1]["text"] == "Читаю файл."
    assert calls and calls[0].name == "read_file" and calls[0].arguments == {"path": "a.py"}
    assert "".join(seen) == "Читаю файл."


def test_anthropic_tool_round_drops_unsigned_thinking_blocks():
    service = AIService({"ai_provider": "anthropic", "ai_model": "claude-opus-5.5",
                         "ai_api_key": "sk-ant-test",
                         "ai_base_url": "https://api.anthropic.com/v1"})

    def stream(_path, _payload):
        yield {"type": "content_block_start", "index": 0,
               "content_block": {"type": "thinking", "thinking": ""}}
        yield {"type": "content_block_delta", "index": 0,
               "delta": {"type": "thinking_delta", "thinking": "мысли без подписи"}}
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "content_block_start", "index": 1,
               "content_block": {"type": "tool_use", "id": "toolu_1",
                                 "name": "read_file", "input": {"path": "a.py"}}}
        yield {"type": "content_block_stop", "index": 1}

    service._stream_json = stream
    blocks, calls = service._anthropic_tool_round(
        [{"role": "user", "content": "прочитай"}], [_read_file_tool()], None, None)

    assert [block["type"] for block in blocks] == ["tool_use"]
    assert calls[0].arguments == {"path": "a.py"}
