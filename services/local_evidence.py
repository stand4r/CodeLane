"""Lossless, request-local storage for tool output; no network or model calls."""
import hashlib
import json

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

    def read(self, id, offset=0, length=None, query=None, queries=None, context_lines=2):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset должен быть целым числом >= 0")
        if length is not None and (type(length) is not int or length < 1):
            raise ValueError("length должен быть положительным целым числом")
        text = self.values[id]
        if query is not None or queries is not None:
            if queries is not None and not isinstance(queries, list):
                raise ValueError("queries должен быть массивом строк")
            terms = ([query] if query is not None else []) + (queries or [])
            if not terms or any(not isinstance(term, str) or not term for term in terms):
                raise ValueError("Укажите непустые строки поиска")
            if type(context_lines) is not int or context_lines < 0:
                raise ValueError("context_lines должен быть целым числом >= 0")
            lines = text.splitlines(keepends=True)
            offsets, position, hits = [], 0, []
            folded = [term.casefold() for term in terms]
            for index, line in enumerate(lines):
                offsets.append(position)
                if position + len(line) > offset and any(term in line.casefold() for term in folded):
                    hits.append(index)
                position += len(line)
            ranges = []
            for index in hits:
                start, end = max(0, index-context_lines), min(len(lines), index+context_lines+1)
                if ranges and start <= ranges[-1][1]:
                    ranges[-1][1] = max(end, ranges[-1][1])
                else:
                    ranges.append([start, end])
            matches = [{"offset": offsets[start], "line": start+1, "text": "".join(lines[start:end])}
                       for start, end in ranges]
            return json.dumps({"matches": matches, "total": len(hits)}, ensure_ascii=False, separators=(",", ":"))
        return text[offset:] if length is None else text[offset:offset + length]

    def preview(self, text, characters):
        key = self.put(text)
        half = max(1, characters // 2)
        return (text[:half] + f"\n[Полный результат сохранён на ПК: read_local_evidence id={key}; символов={len(text)}]\n"
                + text[-half:])
