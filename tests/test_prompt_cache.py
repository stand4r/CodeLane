from services.ai_service import AIService, AIError
from services.agent_harness import AgentUsage


def test_cache_key_is_stable_across_user_turns_and_separates_roles():
    s = AIService({"ai_provider": "openai", "ai_model": "gpt-test"})
    a = s._payload(
        [{"role": "system", "content": "same"}, {"role": "user", "content": "one"}], True
    )
    b = s._payload(
        [{"role": "system", "content": "same"}, {"role": "user", "content": "two"}], True
    )
    assert a["prompt_cache_key"] == b["prompt_cache_key"]
    assert (
        s._payload([{"role": "system", "content": "different"}], True)["prompt_cache_key"]
        != a["prompt_cache_key"]
    )


def test_cache_optional_fields_can_be_disabled_and_rejected_safely():
    s = AIService({"ai_provider": "openai", "ai_prompt_cache": False})
    assert "prompt_cache_key" not in s._payload([], True)
    s = AIService({"ai_provider": "openai"})
    p = s._payload([], True)
    clean = s._fallback_payload(p, AIError("unsupported prompt_cache_key", status=400))
    assert clean is not None and "prompt_cache_key" not in clean
    assert "prompt_cache_key" not in s._payload([], True)


def test_usage_distinguishes_cache_hit_zero_and_unknown():
    a = AgentUsage()
    a.add({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 60}})
    assert (
        a.as_dict()["cache_read_input_tokens"] == 60 and a.as_dict()["uncached_input_tokens"] == 40
    )
    b = AgentUsage()
    b.add({"prompt_tokens": 100})
    assert (
        "cache_read_input_tokens" not in b.as_dict() and "uncached_input_tokens" not in b.as_dict()
    )
    c = AgentUsage()
    c.add({"prompt_tokens": 100, "prompt_cache_hit_tokens": 0})
    assert (
        c.as_dict()["cache_read_input_tokens"] == 0 and c.as_dict()["uncached_input_tokens"] == 100
    )


def test_anthropic_counts_and_reasoning_are_not_doubled():
    a = AgentUsage()
    a.add(
        {
            "input_tokens": 10,
            "cache_read_input_tokens": 90,
            "reasoning_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
    )
    assert a.as_dict()["uncached_input_tokens"] == 10 and a.as_dict()["reasoning_tokens"] == 4


def test_responses_usage_and_aggregate_do_not_double_count_cache():
    from services.ai_budget import input_tokens

    a = AgentUsage()
    a.add({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 70}})
    assert (
        a.as_dict()["cache_read_input_tokens"] == 70 and a.as_dict()["uncached_input_tokens"] == 30
    )
    b = AgentUsage()
    b.add(a.as_dict())
    assert b.as_dict()["uncached_input_tokens"] == 30
    assert input_tokens(b.as_dict()) == 100


def test_evidence_tool_requires_paging_and_returns_targeted_search_results():
    import json

    from services.local_evidence import LocalEvidence, MAX_EVIDENCE_PAGE

    evidence = LocalEvidence()
    text = "".join(f"{i} needle {'x' * 200}\n" for i in range(400))
    key = evidence.put(text)
    page = evidence.read(key, page_limit=MAX_EVIDENCE_PAGE)
    assert page[:MAX_EVIDENCE_PAGE] == text[:MAX_EVIDENCE_PAGE]
    assert f"offset={MAX_EVIDENCE_PAGE}" in page
    first = json.loads(evidence.read(key, query="needle", context_lines=0))
    assert len(first["matches"]) == 7
    second = json.loads(evidence.read(key, offset=first["next_offset"], query="needle", context_lines=0))
    assert second["matches"][0]["line"] == 8


def test_network_timeout_is_retryable(monkeypatch):
    import urllib.request
    import pytest

    def timeout(*args, **kwargs):
        raise TimeoutError("timeout")

    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    with pytest.raises(AIError) as exc:
        AIService({})._open(urllib.request.Request("http://localhost"))
    assert exc.value.retryable


def test_stream_finishes_after_final_usage_without_waiting_for_done(monkeypatch):
    import json

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def __iter__(self):
            yield (
                "data: "
                + json.dumps({"choices": [{"finish_reason": "stop", "delta": {"content": "OK"}}]})
                + "\n"
            ).encode()
            yield (
                "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 10}}) + "\n"
            ).encode()
            raise AssertionError("should not wait for socket EOF after final usage")

    s = AIService({"ai_provider": "openai"})
    monkeypatch.setattr(s, "_open", lambda req: Response())
    events = list(s._stream_json_once("/chat/completions", {}))
    assert len(events) == 2 and events[-1]["usage"]["prompt_tokens"] == 10


def test_agent_can_stop_before_the_provider_sends_headers(monkeypatch):
    from threading import Event, Thread

    service = AIService({"ai_provider": "ollama"})
    started, cancelled, release = Event(), Event(), Event()
    answers = []

    def stream(*_):
        started.set()
        release.wait(3)
        yield {"choices": [{"delta": {"content": "late"}}]}

    monkeypatch.setattr(service, "_stream_json_once", stream)
    worker = Thread(target=lambda: answers.append(service.converse("Hello", should_stop=cancelled.is_set)))
    worker.start()
    try:
        assert started.wait(1)
        cancelled.set()
        worker.join(1)
        assert not worker.is_alive()
        assert answers == [""] and service.last_stop_reason == "stopped"
    finally:
        release.set()
        worker.join(4)


def test_agent_returns_completed_answer_if_provider_does_not_close_sse(monkeypatch):
    from threading import Event
    from time import monotonic

    from services import ai_stream

    service = AIService({"ai_provider": "ollama"})
    release = Event()
    monkeypatch.setattr(ai_stream, "USAGE_GRACE", 0.05)

    def stream(*_):
        yield {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
        release.wait(3)

    monkeypatch.setattr(service, "_stream_json_once", stream)
    started = monotonic()
    try:
        assert service.converse("Hello") == "OK"
        assert monotonic() - started < 1
    finally:
        release.set()
