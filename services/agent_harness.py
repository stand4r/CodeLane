"""PyScribe agent harness: the multi-round tool loop.

This is the piece that turns a chat model into a working coding agent:

* the loop keeps going until the model produces a final answer or the user stops it;
* recent tool results retain more detail; large discovery results are
  shortened in provider requests while the transcript retains them;
* repeated identical tool calls are detected and the model
  is directed to earlier results;
* transient provider errors (429/5xx/network) are retried with backoff;
* token usage is accumulated over all rounds and reported once;
* the user can abort at any moment (``should_stop``) between rounds and calls.

The class deliberately knows nothing about Qt: the panel passes plain callbacks
and receives a plain :class:`AgentResult`, which keeps it testable.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from http.client import IncompleteRead
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from services.ai_service import AIError, AIService, ToolCall

__all__ = ["AgentHarness", "AgentResult", "AgentUsage", "ToolRun"]

class RoundBudget:
    """Deprecated compatibility object; agent runs no longer spend round budgets."""

    def __init__(self, rounds: int):
        self._remaining = rounds
        self._lock = Lock()

    def take(self) -> bool:
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._remaining <= 0

# A single tool result is copied into the transcript as-is. The limit only
# protects the process from a runaway command dumping gigabytes.
MAX_TOOL_RESULT_CHARS = 2_000_000

# How many identical (tool, arguments) calls are allowed before the harness
# answers with the cached result and asks the model to continue.
SAME_CALL_LIMIT = 3
RECENT_DISCOVERY_CHARS = 12_000
MAX_DISCOVERY_RESULT_CHARS = 24_000
RECENT_DISCOVERY_ROUNDS = 1
DISCOVERY_PREVIEW_CHARS = 400
DISCOVERY_TOOLS = frozenset({"read_file", "list_directory", "search_in_project",
                            "editor_state", "workspace_state", "analysis_details",
                            "dependency_graph", "git_read", "browser_read", "browser_search",
                            "assign_team"})
TEXT_OBSERVATIONS = DISCOVERY_TOOLS | {"run_project_tests", "test_results", "terminal_output",
                                       "run_terminal_command", "read_previous_result", "read_local_evidence"}
LOOKUP_TOOL = {"type": "function", "function": {
    "name": "lookup_tools", "description": "Схемы инструментов IDE и MCP по имени или описанию. Пустой запрос — список имён.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 4
RETRY_DELAYS = (0.8, 1.8, 4.0)

USAGE_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "reasoning_tokens",
    "uncached_input_tokens",
)


@dataclass
class AgentUsage:
    """Token usage summed over every request of one answer."""

    values: Dict[str, int] = field(default_factory=dict)

    def add(self, usage: Optional[Dict[str, Any]]) -> None:
        if not isinstance(usage, dict):
            return
        for key in USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                self.values[key] = int(self.values.get(key, 0)) + int(value)
        if "cache_read_input_tokens" not in usage:
            details = usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if cached is None:
                cached = usage.get("prompt_cache_hit_tokens")
            if cached is None and isinstance(usage.get('input_tokens_details'), dict):
                cached = usage['input_tokens_details'].get('cached_tokens')
            if type(cached) is int:
                key = "cache_read_input_tokens"
                self.values[key] = self.values.get(key, 0) + cached
        if 'uncached_input_tokens' not in usage:
            if isinstance(usage.get('prompt_tokens'), (int, float)):
                cached = usage.get('cache_read_input_tokens')
                if cached is None:
                    details = usage.get('prompt_tokens_details') or {}
                    cached = details.get('cached_tokens', usage.get('prompt_cache_hit_tokens'))
                if isinstance(cached, (int, float)):
                    self.values['uncached_input_tokens'] = self.values.get('uncached_input_tokens', 0) + max(0, int(usage['prompt_tokens']-cached))
            elif isinstance(usage.get('input_tokens_details'), dict) and isinstance(usage.get('input_tokens'), (int, float)):
                cached = usage['input_tokens_details'].get('cached_tokens')
                if isinstance(cached, (int, float)):
                    self.values['uncached_input_tokens'] = self.values.get('uncached_input_tokens', 0) + max(0, int(usage['input_tokens']-cached))
            elif 'cache_read_input_tokens' in usage and isinstance(usage.get('input_tokens'), (int, float)):
                self.values['uncached_input_tokens'] = self.values.get('uncached_input_tokens', 0) + int(usage['input_tokens'])
        # OpenAI nests reasoning tokens in completion_tokens_details.
        details = usage.get("completion_tokens_details")
        if 'reasoning_tokens' not in usage and isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            self.values["reasoning_tokens"] = (self.values.get("reasoning_tokens", 0)
                                               + details["reasoning_tokens"])

    def as_dict(self) -> Dict[str, int]:
        return dict(self.values)

    @property
    def empty(self) -> bool:
        return not self.values


@dataclass
class ToolRun:
    """One executed tool call, for the UI and for the transcript."""

    name: str
    arguments: Dict[str, Any]
    output: str
    ok: bool = True
    duration_ms: int = 0
    repeated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments, "ok": self.ok,
                "duration_ms": self.duration_ms, "repeated": self.repeated,
                "output_chars": len(self.output or "")}


@dataclass
class AgentResult:
    text: str
    messages: List[Dict[str, Any]]
    usage: Dict[str, int]
    rounds: int
    tool_runs: List[ToolRun] = field(default_factory=list)
    stop_reason: str = "answer"  # answer | stopped | empty
    truncated: bool = False


class AgentHarness:
    """Run a tool-using conversation turn to completion."""

    def __init__(
        self,
        service: AIService,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_runner: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        *,
        max_rounds: Optional[int] = None,
        on_text: Optional[Callable[[str], None]] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_usage: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_tool_end: Optional[Callable[[ToolRun], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        tool_result_limit: int = MAX_TOOL_RESULT_CHARS,
        unlimited_rounds: bool = False,
        round_budget: Optional[RoundBudget] = None,
    ):
        self.service = service
        self.tools = list(tools or [])
        self._deferred = {}
        names = {t.get("function", {}).get("name") for t in self.tools}
        if len(names) > 18 and names.isdisjoint({"discover_tools", "lookup_tools"}):
            common = {"read_file", "search_in_project", "list_directory", "edit_file", "write_file",
                      "run_terminal_command", "run_project_tests", "assign_team", "ask_user",
                      "delegate_task", "accept_proposal", "optimize_team", "read_local_evidence"}
            self._deferred = {t["function"]["name"]: t for t in self.tools
                              if t.get("function", {}).get("name") and t["function"]["name"] not in common}
            self.tools = [t for t in self.tools if t.get("function", {}).get("name") not in self._deferred]
            self.tools.append(LOOKUP_TOOL)
        from services.local_evidence import LocalEvidence
        self.evidence = getattr(service, "_team_evidence", None) or LocalEvidence()
        if self.tools:
            self.enable_evidence_tool()
        self.tool_runner = tool_runner
        self.on_text = on_text
        self.on_reasoning = on_reasoning
        self.on_usage = on_usage
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        self.should_stop = should_stop or (lambda: False)
        self.tool_result_limit = int(tool_result_limit)
        self._observations = {}

    def enable_evidence_tool(self) -> None:
        from services.local_evidence import READ_EVIDENCE_TOOL

        if not any(t.get("function", {}).get("name") == "read_local_evidence" for t in self.tools):
            self.tools.append(READ_EVIDENCE_TOOL)

    # ------------------------------------------------------------------
    def run(self, messages: List[Dict[str, Any]]) -> AgentResult:
        messages = list(messages)
        self._observations.clear()
        usage = AgentUsage()
        runs: List[ToolRun] = []
        seen_calls: Dict[str, int] = {}
        discovery_results: list[tuple[int, int]] = []
        rounds = 0
        stop_reason = "answer"
        # Text said *before* a tool call is narration ("читаю файл, проверяю…"),
        # not part of the answer: joining it with the final text produced fused,
        # unreadable paragraphs and looked like reasoning left in the chat.
        narration = ""
        answer = ""

        while True:
            if self.should_stop():
                stop_reason = "stopped"
                break
            rounds += 1

            request = self._request_messages(messages, discovery_results, rounds, self.evidence)
            text, calls = self._one_round(request, usage)
            if self.should_stop():
                narration = text or narration
                stop_reason = "stopped"
                break

            if getattr(self.service, "_team_role", None) and self.service.truncated and calls:
                raise AIError("Ответ этапа достиг лимита токенов. Неполные команды пропущены.")

            if not calls:
                answer = text
                if self.service.truncated:
                    answer = self._join(answer,
                                        self._extend_truncated(request, answer, usage))
                if self.should_stop():
                    narration = answer or narration
                    stop_reason = "stopped"
                    break
                stop_reason = "answer" if answer.strip() else "empty"
                answer = answer or narration
                break

            if text.strip():
                narration = text
            messages = self._append_assistant_turn(messages, text, calls)
            for call in calls:
                if self.should_stop():
                    break
                self._handle_call(messages, call, runs, seen_calls)
                if call.name not in DISCOVERY_TOOLS and call.name != "read_local_evidence":
                    # An edit/command can change files. Allow a fresh read even
                    # if the same range was already requested several times.
                    seen_calls.clear()
                if call.name in TEXT_OBSERVATIONS and runs[-1].ok:
                    discovery_results.append((len(messages) - 1, rounds))

        if stop_reason == "stopped":
            answer = answer or narration
        if not answer.strip() and stop_reason == "answer":
            stop_reason = "empty"
        return AgentResult(
            text=answer,
            messages=messages,
            usage=usage.as_dict(),
            rounds=rounds,
            tool_runs=runs,
            stop_reason=stop_reason,
            truncated=bool(self.service.truncated),
        )

    @staticmethod
    def _request_messages(messages: List[Dict[str, Any]],
                          discovery_results: list[tuple[int, int]],
                          round_number: int, evidence=None) -> List[Dict[str, Any]]:
        request = list(messages)
        # Keep file contents available across rounds while the request fits.
        # Age alone must not force another network round trip to reread a file.
        from services.ai_context import PROJECT_CONTEXT_BYTES
        if evidence is None and len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) <= PROJECT_CONTEXT_BYTES:
            return request
        remaining = RECENT_DISCOVERY_CHARS
        names = {c.get('id'):c.get('function', {}).get('name')
                 for m in messages for c in m.get('tool_calls', [])}
        for message in messages:
            if isinstance(message.get('content'), list):
                for item in message['content']:
                    if isinstance(item, dict) and item.get('type') == 'tool_use':
                        names[item.get('id')] = item.get('name')
        source_remaining = 24_000
        for index, result_round in reversed(discovery_results):
            message = messages[index]
            content = message["content"]
            block = content[0] if isinstance(content, list) else None
            output = block["content"] if block is not None else content
            size = len(output)
            call_id = block.get('tool_use_id') if block is not None else message.get('tool_call_id')
            if names.get(call_id) == 'read_file' and round_number - result_round <= 2 and size <= source_remaining:
                source_remaining -= size
                continue
            if result_round == round_number - 1 and names.get(call_id) == "read_local_evidence":
                continue  # Deliver the exact range the model just requested.
            if result_round == round_number - 1:
                limit = MAX_DISCOVERY_RESULT_CHARS
            elif round_number - result_round <= RECENT_DISCOVERY_ROUNDS and size <= remaining:
                limit = remaining
            else:
                limit = DISCOVERY_PREVIEW_CHARS
            if size <= limit:
                remaining -= size
                continue
            half = limit // 2
            remaining -= limit
            preview = (
                output[:half] + f"\n… [Сокращён результат: пропущено {size - 2 * half} символов. "
                "При необходимости запроси только нужный фрагмент.]\n" + output[-half:]
            )
            if evidence is not None:
                preview = evidence.preview(output, limit)
            if block is not None:
                preview = [{**block, "content": preview}]
            request[index] = {**message, "content": preview}
        return AgentHarness._archive_completed_edits(request, evidence)

    @staticmethod
    def _archive_completed_edits(messages, evidence):
        """Keep exact patches locally once acknowledged; preserve tool-call groups."""
        if evidence is None:
            return messages
        results = {m.get('tool_call_id'): m for m in messages if m.get('role') == 'tool'}
        turns = [i for i, m in enumerate(messages) if m.get('tool_calls')]
        eligible = set(turns[:-2])
        replacements, removed = {}, set()
        for index in eligible:
            calls = messages[index]['tool_calls']
            if any(c.get('function', {}).get('name') not in {'edit_file', 'write_file'} for c in calls):
                continue
            outputs = [results.get(c.get('id')) for c in calls]
            if any(not m or not isinstance(m.get('content'), str) or not m['content'].startswith(('Изменён файл', 'Обновлён файл', 'Создан файл')) for m in outputs):
                continue
            payload = json.dumps({'tool_calls': calls, 'results': outputs}, ensure_ascii=False)
            if len(payload) < 1200:
                continue
            key = evidence.put(payload)
            replacements[index] = {'role': 'assistant', 'content': 'Подтверждённые правки: ' + '; '.join(m['content'] for m in outputs) + f' Полные исторические аргументы: read_local_evidence(id={key!r}). Для текущего состояния читай файл.'}
            removed.update(c['id'] for c in calls)
        return [replacements.get(i, m) for i, m in enumerate(messages) if not (m.get('role') == 'tool' and m.get('tool_call_id') in removed)]

    @staticmethod
    def _join(first: str, second: str) -> str:
        """Join an auto-continued answer with the text it continues."""
        first = (first or "").rstrip()
        second = (second or "").lstrip()
        if not first:
            return second
        if not second:
            return first
        return first + " " + second

    # ------------------------------------------------------------------
    # One request + transient-error retries
    # ------------------------------------------------------------------
    def _one_round(self, messages: List[Dict[str, Any]],
                   usage: AgentUsage) -> tuple[str, List[ToolCall]]:
        for definition in getattr(self.service, '_discovered_tools', ()):
            name = definition.get('function', {}).get('name')
            if name and not any(d.get('function', {}).get('name') == name for d in self.tools):
                self.tools.append(definition)
        last_error: Optional[Exception] = None
        for attempt in range(RETRY_ATTEMPTS):
            if self.should_stop():
                return "", []
            try:
                if self.service.is_anthropic:
                    blocks, calls = self.service._anthropic_tool_round(
                        messages, self.tools, self._text_callback, self._reasoning_callback)
                    usage.add(self.service.last_usage)
                    if self.on_usage:
                        self.on_usage(dict(self.service.last_usage))
                    text = "".join(
                        str(block.get("text") or "")
                        for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text")
                    if calls:
                        # Keep the assistant turn (text + tool_use) for the next round.
                        self._pending_blocks = blocks
                    return text, calls
                text, calls = self.service._openai_tool_round(
                    messages, self.tools, self._text_callback, self._reasoning_callback)
                usage.add(self.service.last_usage)
                if self.on_usage:
                    self.on_usage(dict(self.service.last_usage))
                return text, calls
            except (AIError, ConnectionError, IncompleteRead, TimeoutError) as exc:
                last_error = exc if isinstance(exc, AIError) else AIError(
                    f"Соединение с API прервано: {exc}", retryable=True)
                if not self._is_retryable(last_error) or attempt >= RETRY_ATTEMPTS - 1:
                    if last_error is exc:
                        raise
                    raise last_error from exc
                self._sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
        raise last_error or AIError("Не удалось выполнить запрос к модели.")

    def _text_callback(self, piece: str) -> None:
        if self.on_text and not self.should_stop():
            self.on_text(piece)

    def _reasoning_callback(self, piece: str) -> None:
        if self.on_reasoning and not self.should_stop():
            self.on_reasoning(piece)

    @staticmethod
    def _is_retryable(exc: AIError) -> bool:
        if getattr(exc, "retryable", False):
            return True
        status = getattr(exc, "status", None)
        if status in RETRYABLE_STATUS:
            return True
        text = str(exc).lower()
        return any(token in text for token in (
            "429", "500", "502", "503", "504", "timed out", "timeout",
            "connection", "соединен", "времен", "overloaded", "rate limit"))

    def _sleep(self, seconds: float) -> None:
        """Sleep in small slices so a stop request is honoured quickly."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.should_stop():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    # ------------------------------------------------------------------
    # Message bookkeeping
    # ------------------------------------------------------------------
    def _append_assistant_turn(self, messages: List[Dict[str, Any]],
                               text: str, calls: List[ToolCall]) -> List[Dict[str, Any]]:
        messages = list(messages)
        if self.service.is_anthropic:
            blocks = getattr(self, "_pending_blocks", None) or []
            if not blocks:
                blocks = []
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in calls:
                    blocks.append({"type": "tool_use", "id": call.id,
                                   "name": call.name, "input": call.arguments})
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({
                "role": "assistant",
                "content": text or "",
                "tool_calls": [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.name,
                                  "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                    for call in calls],
            })
        return messages

    def _handle_call(self, messages: List[Dict[str, Any]], call: ToolCall,
                     runs: List[ToolRun], seen_calls: Dict[str, int]) -> None:
        signature = call.name + "|" + json.dumps(call.arguments or {},
                                                 sort_keys=True, ensure_ascii=False)
        seen_calls[signature] = seen_calls.get(signature, 0) + 1
        repeated = call.name != "read_local_evidence" and seen_calls[signature] > SAME_CALL_LIMIT

        if repeated:
            output = self._repeat_notice(call)
            self._append_tool_result(messages, call, output)
            runs.append(ToolRun(call.name, call.arguments, output, ok=False, repeated=True))
            return

        if self.on_tool_start:
            try:
                self.on_tool_start(call.name, call.arguments)
            except Exception:  # noqa: BLE001 - a UI callback must not kill the run
                pass

        started = time.perf_counter()
        ok = True
        if call.name == "lookup_tools" and self._deferred:
            query = str(call.arguments.get("query", "")).strip().casefold()
            matches = [name for name, tool in self._deferred.items()
                       if query in name.casefold() or query in tool["function"].get("description", "").casefold()]
            if query in self._deferred:
                matches = [query]
            if query and len(matches) <= 5:
                self.tools.extend(self._deferred[name] for name in matches if self._deferred[name] not in self.tools)
            output = json.dumps(matches, ensure_ascii=False)
            if query and len(matches) > 5:
                output += " Уточни имя для получения схемы."
        elif call.name == "read_local_evidence":
            try:
                output = self.evidence.read(**call.arguments)
            except (KeyError, TypeError, ValueError) as exc:
                output, ok = f"Ошибка чтения локального результата: {exc}", False
        elif self.tool_runner is None:
            output = f"Инструмент {call.name} недоступен."
            ok = False
        else:
            try:
                output = self.tool_runner(call.name, call.arguments)
            except Exception as exc:  # noqa: BLE001 - report, keep the loop alive
                output = f"Ошибка инструмента {call.name}: {exc}"
                ok = False
        duration_ms = int((time.perf_counter() - started) * 1000)

        image_data = output if isinstance(output, dict) and "__image__" in output else None
        text = ("Снимок экрана передан модели." if image_data else
                "" if output is None else str(output))
        if call.name != "read_local_evidence" and len(text) > self.tool_result_limit:
            text = self.evidence.preview(text, self.tool_result_limit)
        if ok and call.name in {"read_file", "search_in_project"} and len(text) > 1200:
            previous = self._observations.get(signature)
            if previous and previous[0] == text:
                # Still execute the tool to check freshness, but send the same
                # payload only once. The full text remains recoverable locally.
                key = self.evidence.put(text)
                text = (f"Результат не изменился с вызова {previous[1]}; "
                        "используй текст выше без повторного чтения. "
                        f"Полный результат: read_local_evidence(id={key!r}).")
            else:
                self._observations[signature] = (text, call.id)
        self._append_tool_result(messages, call, text)
        if image_data:
            data = image_data["__image__"]
            mime = image_data.get("media_type", "image/png")
            if self.service.is_anthropic:
                messages[-1]["content"][0]["content"] = [
                    {"type": "text", "text": text},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                ]
            else:
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Снимок экрана после вызова capture_screen."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ]})
        run = ToolRun(call.name, call.arguments, text, ok=ok, duration_ms=duration_ms)
        runs.append(run)
        if self.on_tool_end:
            try:
                self.on_tool_end(run)
            except Exception:  # noqa: BLE001
                pass

    def _append_tool_result(self, messages: List[Dict[str, Any]], call: ToolCall,
                            output: str) -> None:
        if self.service.is_anthropic:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": output}
            ]})
        else:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    @staticmethod
    def _repeat_notice(call: ToolCall) -> str:
        return (
            f"Инструмент {call.name} уже вызывался с этими аргументами "
            f"{SAME_CALL_LIMIT} раза; результат выше в истории. Повторный вызов не выполнен. "
            "Используй уже полученные данные и продолжай работу."
        )

    # ------------------------------------------------------------------
    def _extend_truncated(self, messages: List[Dict[str, Any]], partial: str,
                          usage: AgentUsage) -> str:
        """Continue a text answer that hit the provider's output ceiling."""
        def record_usage(value):
            usage.add(value)
            if self.on_usage:
                self.on_usage(dict(value))

        working = list(messages) + [
            {"role": "assistant", "content": partial},
            {"role": "user", "content": self.service._continue_prompt()},
        ]
        return self.service._stream_until_complete(
            working, self._text_callback, self._reasoning_callback,
            on_usage=record_usage, should_stop=self.should_stop,
        )
