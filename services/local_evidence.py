"""Lossless, request-local storage for tool output; no network or model calls."""
import hashlib
import json
import re
from collections import deque

MAX_EVIDENCE_PAGE = 48_000

READ_EVIDENCE_TOOL = {"type": "function", "function": {
    "name": "read_local_evidence",
    "description": "Текст по id. query/queries ищут строки, offset+length читают диапазон.",
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "query": {"type": "string", "description": "Точный текст для локального поиска. Возвращает строки совпадений и смещения без чтения всего результата."},
        "queries": {"type": "array", "items": {"type": "string"}, "description": "Несколько точных строк для поиска за один вызов (ИЛИ)."},
        "context_lines": {"type": "integer", "minimum": 0, "description": "Число соседних строк вокруг совпадения, по умолчанию 2."},
        "length": {"type": "integer", "minimum": 1}}, "required": ["id"]}}}

class LocalEvidence:
    def __init__(self):
        self.values = {}

    def put(self, text):
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.values.setdefault(key, text)
        return key

    def read(self, id, offset=0, length=None, query=None, queries=None, context_lines=2,
             *, page_limit=None, should_stop=None):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset должен быть целым числом >= 0")
        if length is not None and (type(length) is not int or length < 1):
            raise ValueError("length должен быть положительным целым числом")
        text = self.values[id]
        if query is not None or queries is not None:
            if queries is not None and not isinstance(queries, list):
                raise ValueError("queries должен быть массивом строк")
            terms = ([query] if query is not None else []) + (queries or [])
            if not terms or len(terms) > 24 or any(not isinstance(term, str) or not term for term in terms):
                raise ValueError("Укажите до 24 непустых строк поиска")
            if type(context_lines) is not int or not 0 <= context_lines <= 10:
                raise ValueError("context_lines должен быть от 0 до 10")
            return self._search(text, offset, terms, context_lines, page_limit or MAX_EVIDENCE_PAGE, should_stop)
        if page_limit is None:
            return text[offset:] if length is None else text[offset:offset + length]
        size = min(length or page_limit, page_limit)
        end = offset + size
        result = text[offset:end]
        if end < len(text):
            result += f"\n[Продолжение: read_local_evidence(id={id!r}, offset={end}).]"
        return result

    @staticmethod
    def _search(text, offset, terms, context_lines, page_limit, should_stop):
        matches = []
        remaining = page_limit
        folded = [term.casefold() for term in terms]
        previous = deque(maxlen=context_lines)
        following = 0
        next_offset = None
        last_added = -1
        hits = 0
        for number, match in enumerate(re.finditer(r"[^\n]*\n|[^\n]+$", text), 1):
            if should_stop and should_stop():
                raise ValueError("Запрос остановлен.")
            line = match.group()
            lower = line.casefold() if match.end() > offset else ""
            position = next((pos for term in folded if (pos := lower.find(term)) >= 0), -1)
            hit = position >= 0
            if hit:
                if hits >= 7 or remaining < 3000:
                    next_offset = match.start()
                    break
                hits += 1
            start = min(max(0, position - 100), max(0, len(line) - 400))
            current = {"line": number, "offset": match.start() + start, "text": line[start:start + 400]}
            if hit or following:
                candidates = [*previous, current] if hit else [current]
                reserved = len(json.dumps(current, ensure_ascii=False)) + 2 if hit else 0
                for candidate in candidates:
                    if candidate["line"] <= last_added:
                        continue
                    size = len(json.dumps(candidate, ensure_ascii=False)) + 2
                    if size + (0 if candidate is current else reserved) > remaining:
                        continue
                    matches.append(candidate)
                    remaining -= size
                    last_added = candidate["line"]
            following = context_lines if hit else max(0, following - 1)
            previous.append(current)
        return json.dumps({"matches": matches, "next_offset": next_offset}, ensure_ascii=False)

    def preview(self, text, characters):
        key = self.put(text)
        marker = f"\n[Полный результат сохранён на ПК: read_local_evidence id={key}; символов={len(text)}]\n"
        half = max(0, (characters - len(marker)) // 2)
        return text[:half] + marker + (text[-half:] if half else "")
