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

import hashlib
import json
import time
from dataclasses import dataclass, field
from http.client import IncompleteRead
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from core.logging_config import get_logger
from services.ai_budget import CONTEXT_COMPACTION_BYTES, input_tokens, request_size, total_tokens
from services.ai_service import AIError, AIService, ToolCall
from services.local_evidence import MAX_EVIDENCE_PAGE, LocalEvidence
from services.agent_context import AgentContext, CONTEXT_TOOLS, CONTEXT_TOOL_NAMES

logger = get_logger("ai_agent")

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
SAME_CALL_LIMIT = 8
STALLED_ROUND_LIMIT = 20
STALLED_NOTICE = "Работа остановлена. Несколько раундов подряд не дали новых результатов или изменений."
RECENT_TOOL_CHARS = 60_000
DISCOVERY_PREVIEW_CHARS = 800
STATE_CHANGING_TOOLS = frozenset({
    "apply_patch", "exec_command", "write_stdin",
    "edit_file", "write_file", "editor_save", "explorer_edit", "todo_edit", "project_config_edit",
    "run_terminal_command", "terminal_execute", "terminal_control", "git_change",
    "python", "git", "pytest", "pip",
    "assign_team", "delegate_task", "browser_control", "browser_navigate",
})
FILE_READ_TOOLS = frozenset({"read_file", "read_document"})
# These actions may modify files even when they fail or return no output.
FILE_READ_INVALIDATORS = STATE_CHANGING_TOOLS | frozenset({
    "run_project_tests", "run_executable", "open_application", "run_admin_command",
    "accept_proposal", "reconcile_edit", "finish_stage", "ask_user", "apply_patch",
    "exec_command", "write_stdin",
})
LOOKUP_TOOL = {"type": "function", "function": {
    "name": "lookup_tools", "description": "Схемы дополнительных инструментов по имени или описанию. Для вызова используй call_tool.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}
CALL_TOOL = {"type": "function", "function": {
    "name": "call_tool", "description": "Вызвать дополнительный инструмент по схеме из lookup_tools.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}, "arguments": {"type": "object", "additionalProperties": True}},
        "required": ["name", "arguments"]}}}



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
        incoming = input_tokens(usage)
        if incoming is not None:
            self.values["measured_input_tokens"] = self.values.get("measured_input_tokens", 0) + incoming
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
    stop_reason: str = "answer"  # answer | stopped | stalled | empty
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
        on_request: Optional[Callable[[], None]] = None,
        read_version: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        self.service = service
        self.tools = list(tools or [])
        self._deferred = {}
        names = {t.get("function", {}).get("name") for t in self.tools}
        if len(names) > 18 and names.isdisjoint({"lookup_tools", "call_tool"}):
            common = {"read_file", "read_document", "search_in_project", "list_directory", "edit_file", "write_file",
                      "find_files", "apply_patch", "exec_command", "write_stdin", "view_image",
                      "run_terminal_command", "terminal_execute", "terminal_output", "terminal_control", "run_project_tests",
                      "assign_team", "ask_user",
                      "delegate_task", "accept_proposal", "optimize_team", "read_local_evidence",
                      "finish_stage", "discover_tools", "read_previous_result"}
            self._deferred = {t["function"]["name"]: t for t in self.tools
                              if t.get("function", {}).get("name") and t["function"]["name"] not in common}
            self.tools = [t for t in self.tools if t.get("function", {}).get("name") not in self._deferred]
            self.tools.extend((LOOKUP_TOOL, CALL_TOOL))
        self.evidence = getattr(service, "_team_evidence", None) or LocalEvidence()
        if self.tools:
            self.enable_evidence_tool()
            self.tools.extend(tool for tool in CONTEXT_TOOLS if tool["function"]["name"] not in names)
        self.tool_runner = tool_runner
        self.on_text = on_text
        self.on_reasoning = on_reasoning
        self.on_usage = on_usage
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        self.on_request = on_request
        self.should_stop = should_stop or (lambda: False)
        self.tool_result_limit = int(tool_result_limit)
        self._observations = {}
        self._file_reads = {}
        self.context = AgentContext()
        self.read_version = read_version

    def enable_evidence_tool(self) -> None:
        from services.local_evidence import READ_EVIDENCE_TOOL

        if not any(t.get("function", {}).get("name") == "read_local_evidence" for t in self.tools):
            self.tools.append(READ_EVIDENCE_TOOL)

    # ------------------------------------------------------------------
    def run(self, messages: List[Dict[str, Any]]) -> AgentResult:
        messages = list(messages)
        self._observations.clear()
        self._file_reads.clear()
        self.context = AgentContext()
        usage = self.usage = AgentUsage()
        runs: List[ToolRun] = []
        seen_calls: Dict[str, int] = {}
        observations = set()
        stalled_rounds = 0
        tool_results: list[tuple[int, int]] = []
        previews = {}
        turn_starts = []
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
            request = self._request_messages(
                messages, tool_results, rounds + 1, self.evidence, turn_starts, self.tools, previews,
                context=self.context,
            )
            self.context.request_bytes = request_size(request, self.tools)
            text, calls = self._one_round(request, usage)
            rounds += 1
            if self.should_stop():
                narration = text or narration
                stop_reason = "stopped"
                break

            if self.service.truncated and calls:
                raise AIError("Ответ достиг лимита токенов. Неполные команды пропущены.")

            if not calls:
                answer = text
                if self.service.truncated:
                    answer = self._join(answer, self._extend_truncated(request, answer, usage))
                if self.should_stop():
                    narration = answer or narration
                    stop_reason = "stopped"
                    break
                stop_reason = "answer" if answer.strip() else "empty"
                answer = answer or narration
                break

            if text.strip():
                narration = text
            turn_starts.append(len(messages))
            messages = self._append_assistant_turn(messages, text, calls)
            images = []
            progressed = False
            for call in calls:
                if self.should_stop():
                    break
                resolved = self._resolve_call(call)
                result_index = len(messages)
                signature, run = self._handle_call(messages, resolved, runs, seen_calls)
                if len(messages) > result_index + 1:
                    images.append(messages.pop())
                tool_results.append((result_index, rounds))
                if resolved.name in {"exec_command", "write_stdin"} and run.ok:
                    try:
                        if json.loads(run.output).get("completed") is False:
                            progressed = True  # A bounded command is still making progress in the OS.
                    except (ValueError, AttributeError):
                        pass
                if run.ok and not run.repeated and run.output and not run.output.startswith((
                    "Результат не изменился", "Файл уже содержит запрошенный текст",
                )):
                    digest = hashlib.sha256(run.output.encode("utf-8", errors="replace")).digest()
                    observation = (resolved.name, signature, digest)
                    if observation in observations:
                        continue
                    progressed = True
                    observations.add(observation)
                    if resolved.name in STATE_CHANGING_TOOLS:
                        seen_calls.clear()
                        observations = {item for item in observations if item[0] in STATE_CHANGING_TOOLS}
            messages.extend(images)
            stalled_rounds = 0 if progressed else stalled_rounds + 1
            if stalled_rounds >= STALLED_ROUND_LIMIT and not self.should_stop():
                stop_reason = "stalled"
                answer = STALLED_NOTICE
                logger.info("AI tool loop stalled: rounds=%d recent_tools=%s",
                            rounds, ",".join(run.name for run in runs[-8:]))
                break

        if stop_reason == "stopped":
            answer = answer or narration
        measured = usage.as_dict()
        incoming = input_tokens(measured)
        cached = measured.get("cache_read_input_tokens")
        logger.info("AI task: rounds=%d input_tokens=%s cached_tokens=%s cache_hit_rate=%s total_tokens=%s stop=%s",
                    rounds, incoming, cached, f"{cached / incoming:.1%}" if incoming and cached is not None else None,
                    total_tokens(measured), stop_reason)
        if not answer.strip() and stop_reason == "answer":
            stop_reason = "empty"
        return AgentResult(
            text=answer,
            messages=messages,
            usage=measured,
            rounds=rounds,
            tool_runs=runs,
            stop_reason=stop_reason,
            truncated=bool(self.service.truncated),
        )

    @staticmethod
    def _request_messages(messages, tool_results, round_number, evidence=None, turn_starts=(), tools=(),
                          previews=None, context=None):
        request = list(messages)
        evidence = evidence if evidence is not None else LocalEvidence()
        previews = previews if previews is not None else {}
        context = context if context is not None else AgentContext()
        force = context.force_compaction
        # Keep the first observation and reference it in later identical results.
        # File reads are reused within a run until an action invalidates them.
        first_observation = {}
        for index, _ in tool_results:
            message = messages[index]
            preceding = messages[index - 1] if index else {}
            calls = preceding.get("tool_calls", [])
            if not any(call.get("id") == message.get("tool_call_id") and
                       call.get("function", {}).get("name") in
                       {"read_file", "read_document", "search_in_project", "list_directory"}
                       for call in calls):
                continue
            content = message["content"]
            block = content[0] if isinstance(content, list) else None
            output = block["content"] if block is not None else content
            if not isinstance(output, str):
                continue
            first = first_observation.setdefault(output, message.get("tool_call_id", str(index)))
            if first != message.get("tool_call_id", str(index)):
                evidence_id = evidence.put(output)
                reference = (f"Same result as tool call {first}; "
                             f"full result: read_local_evidence(id='{evidence_id}').")
                request[index] = {**message, "content": [{**block, "content": reference}] if block else reference}
        remaining = RECENT_TOOL_CHARS
        for index, result_round in reversed(tool_results):
            if request[index] is not messages[index]:
                continue
            message = messages[index]
            content = message["content"]
            block = content[0] if isinstance(content, list) else None
            output = block["content"] if block is not None else content
            if not isinstance(output, str):
                continue
            recent = result_round == round_number - 1
            limit = max(DISCOVERY_PREVIEW_CHARS, min(MAX_EVIDENCE_PAGE + 160, remaining)) if recent else DISCOVERY_PREVIEW_CHARS
            preview = output
            if len(output) > limit:
                key = (index, limit)
                if key not in previews:
                    previews[key] = evidence.preview(output, limit)
                preview = previews[key]
            if recent:
                remaining -= len(preview)
            if preview != output:
                request[index] = {**message, "content": [{**block, "content": preview}] if block else preview}

        if not force and request_size(request, tools) <= CONTEXT_COMPACTION_BYTES:
            return request
        compact = AgentHarness._archive_completed_edits(request, evidence)
        if not force and request_size(compact, tools) <= CONTEXT_COMPACTION_BYTES:
            return compact
        if turn_starts:
            # Drop whole exchanges to preserve the provider's tool call/result
            # pairing. The original task and any attached data stay in place.
            prefix = request[:turn_starts[0]]
            starts = turn_starts[-1:] if force else turn_starts[1:]
            for start in starts:
                archived = messages[turn_starts[0]:start]
                note = {"role": "assistant", "content": context.checkpoint(archived, evidence, commit=False)}
                candidate = prefix + [note] + request[start:]
                if start == turn_starts[-1] or request_size(candidate, tools) <= CONTEXT_COMPACTION_BYTES:
                    note["content"] = context.checkpoint(archived, evidence)
                    return candidate
        return compact

    @staticmethod
    def _archive_completed_edits(messages, evidence):
        """Keep exact patches locally once acknowledged and preserve tool-call groups."""
        turns = [i for i, message in enumerate(messages) if message.get("tool_calls")]
        replacements, removed = {}, set()
        for index, end in zip(turns[:-2], turns[1:-1]):
            calls = messages[index]["tool_calls"]
            if any(call["function"]["name"] not in {"edit_file", "write_file"} for call in calls):
                continue
            responses = [(i, messages[i]) for i in range(index + 1, end) if messages[i].get("role") == "tool"]
            if len(responses) != len(calls) or {message["tool_call_id"] for _, message in responses} != {
                call["id"] for call in calls
            }:
                continue
            outputs = [message for _, message in responses]
            if any(not output["content"].startswith(("Изменён файл", "Обновлён файл", "Создан файл"))
                   for output in outputs):
                continue
            payload = json.dumps({"tool_calls": calls, "results": outputs}, ensure_ascii=False)
            if len(payload) < 1200:
                continue
            key = evidence.put(payload)
            summary = "\n".join(output["content"] for output in outputs)
            replacements[index] = {"role": "assistant", "content": (
                f"Подтверждённые правки.\n{summary}\nАргументы доступны через read_local_evidence(id={key!r})."
            )}
            removed.update(i for i, _ in responses)
        return [replacements.get(i, message) for i, message in enumerate(messages) if i not in removed]

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
        for attempt in range(RETRY_ATTEMPTS):
            if self.should_stop():
                return "", []
            self.service.last_usage = {}
            text = ""
            started = time.monotonic()
            if self.on_request:
                self.on_request()
            try:
                if self.service.is_anthropic:
                    blocks, calls = self.service._anthropic_tool_round(
                        messages, self.tools, self._text_callback, self._reasoning_callback)
                    text = "".join(
                        str(block.get("text") or "")
                        for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text")
                    if calls:
                        self._pending_blocks = blocks
                else:
                    text, calls = self.service._openai_tool_round(
                        messages, self.tools, self._text_callback, self._reasoning_callback)
                return text, calls
            except (AIError, ConnectionError, IncompleteRead, TimeoutError) as exc:
                error = exc if isinstance(exc, AIError) else AIError(
                    f"Соединение с API прервано: {exc}", retryable=True)
                logger.warning("AI request failed: attempt=%d error=%s", attempt + 1, error)
                if not self._is_retryable(error) or attempt >= RETRY_ATTEMPTS - 1:
                    if error is exc:
                        raise
                    raise error from exc
            finally:
                value = self.service.last_usage
                usage.add(value)
                logger.info("AI request: attempt=%d seconds=%.2f input_tokens=%s",
                            attempt + 1, time.monotonic() - started, input_tokens(value))
                if self.on_usage and value:
                    self.on_usage(dict(value))
            self._sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

    def _resolve_call(self, call):
        if call.name == "call_tool" and isinstance(call.arguments, dict):
            name, args = call.arguments.get("name"), call.arguments.get("arguments")
            if isinstance(name, str) and name in self._deferred and isinstance(args, dict):
                return ToolCall(call.id, name, args)
        return call

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
            assistant_turn: Dict[str, Any] = {
                "role": "assistant",
                "content": text or "",
                "tool_calls": [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.name,
                                  "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                    for call in calls],
            }
            # Thinking-mode providers must receive the reasoning of the turn
            # back together with its tool calls, otherwise they answer 400.
            # DeepSeek thinks by default and wants the field even when the
            # round produced no visible reasoning at all (empty string).
            reasoning = (getattr(self.service, "last_reasoning", "") or "").strip()
            if reasoning or getattr(self.service, "reasoning_required", False):
                assistant_turn["reasoning_content"] = reasoning
            messages.append(assistant_turn)
        return messages

    def _handle_call(self, messages: List[Dict[str, Any]], call: ToolCall,
                     runs: List[ToolRun], seen_calls: Dict[str, int]) -> tuple[str, ToolRun]:
        args = dict(call.arguments or {})
        if call.name in FILE_READ_TOOLS:
            # Explicit defaults must not bypass the duplicate-read guard.
            try:
                if call.name == "read_file":
                    args["offset"] = max(1, int(args.get("offset") or 1))
                    args["limit"] = 200 if args.get("limit") is None else max(0, int(args["limit"]))
                    args["column"] = max(0, int(args.get("column", 0)))
                else:
                    args.setdefault("offset", 1)
                    args.setdefault("limit", 5)
            except (TypeError, ValueError, OverflowError):
                # Let the tool report invalid arguments through its usual error path.
                args = dict(call.arguments or {})
        cacheable = True
        signature_args = args
        if call.name in FILE_READ_TOOLS and self.read_version is not None:
            try:
                signature_args = {"arguments": args, "version": self.read_version(call.name, args)}
            except Exception:
                # Missing files, closing UI or changed permissions must reach the real tool.
                cacheable = False
        arguments = json.dumps(signature_args, sort_keys=True, ensure_ascii=False)
        signature = hashlib.sha256(f"{call.name}|{arguments}".encode("utf-8", errors="replace")).hexdigest()
        previous_read = self._file_reads.get(signature) if cacheable else None
        if previous_read is not None:
            first_call, evidence_id = previous_read
            output = (f"Этот фрагмент уже прочитан в вызове {first_call}. Повторное чтение не выполнено. "
                      "Используй полученные данные и продолжай работу. "
                      f"Сохранённый результат: read_local_evidence(id={evidence_id!r}).")
            self._append_tool_result(messages, call, output)
            run = ToolRun(call.name, call.arguments, output, repeated=True)
            self._record_tool_run(runs, run)
            return signature, run
        seen_calls[signature] = seen_calls.get(signature, 0) + 1
        repeated = call.name != "write_stdin" and seen_calls[signature] > SAME_CALL_LIMIT

        if repeated:
            output = self._repeat_notice(call)
            self._append_tool_result(messages, call, output)
            run = ToolRun(call.name, call.arguments, output, ok=False, repeated=True)
            self._record_tool_run(runs, run)
            return signature, run

        if self.on_tool_start:
            try:
                self.on_tool_start(call.name, call.arguments)
            except Exception:  # noqa: BLE001 - a UI callback must not kill the run
                pass

        started = time.perf_counter()
        if call.name in FILE_READ_INVALIDATORS:
            self._file_reads.clear()
        ok = True
        if call.name in CONTEXT_TOOL_NAMES:
            try:
                output = self.context.call(call.name, call.arguments, self.evidence)
            except (TypeError, ValueError) as exc:
                output, ok = f"Ошибка контекста: {exc}", False
        elif call.name == "lookup_tools" and self._deferred:
            query = str(call.arguments.get("query", "")).strip().casefold()
            matches = [name for name, tool in self._deferred.items()
                       if query in name.casefold() or query in tool["function"].get("description", "").casefold()]
            if query in self._deferred:
                matches = [query]
            output = json.dumps([self._deferred[name]["function"] for name in matches]
                                if query and len(matches) <= 5 else matches, ensure_ascii=False)
            if not query or len(matches) > 5:
                output += " Уточни имя для получения схемы."
            ok = bool(matches)
        elif call.name == "call_tool":
            output, ok = "Ошибка: укажи имя доступного инструмента и аргументы из его схемы.", False
        elif call.name == "read_local_evidence":
            try:
                output = self.evidence.read(**call.arguments, page_limit=MAX_EVIDENCE_PAGE,
                                            should_stop=self.should_stop)
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
        text = (image_data.get("description", "Снимок экрана передан модели.") if image_data else
                "" if output is None else str(output))
        if text.startswith(("Ошибка MCP CodeLane:", "Ошибка:", "Ошибка правки:", "Неизвестный инструмент.")):
            ok = False
        if call.name == "apply_patch" and ok:
            try:
                ok = json.loads(text).get("ok", False)
            except (ValueError, AttributeError):
                ok = False
        if ok and cacheable and call.name in FILE_READ_TOOLS and not image_data:
            self._file_reads[signature] = (call.id, self.evidence.put(text))
        if call.name != "read_local_evidence" and len(text) > self.tool_result_limit:
            text = self.evidence.preview(text, self.tool_result_limit)
        if ok and call.name == "search_in_project" and len(text) > 1200:
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
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ]})
        run = ToolRun(call.name, call.arguments, text, ok=ok, duration_ms=duration_ms)
        self._record_tool_run(runs, run)
        return signature, run

    def _record_tool_run(self, runs, run):
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
            if self.on_usage and value:
                self.on_usage(dict(value))

        working = list(messages) + [
            {"role": "assistant", "content": partial[-12_000:]},
            {"role": "user", "content": self.service._continue_prompt()},
        ]
        return self.service._stream_until_complete(
            working, self._text_callback, self._reasoning_callback,
            on_usage=record_usage, should_stop=self.should_stop,
        )
