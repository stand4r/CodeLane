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
    a = AgentUsage()
    a.add({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 70}})
    assert (
        a.as_dict()["cache_read_input_tokens"] == 70 and a.as_dict()["uncached_input_tokens"] == 30
    )
    b = AgentUsage()
    b.add(a.as_dict())
    assert b.as_dict()["uncached_input_tokens"] == 30


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
