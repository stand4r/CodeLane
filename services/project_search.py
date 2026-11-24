"""Bounded local search: one model call can locate several related symbols."""
from pathlib import Path
import re


def search_project(root, *, query="", queries=None, glob="", regex=False,
                   context_lines=2, max_results=40, should_stop=None):
    # Imported lazily because the toolbox routes searches here.
    from services.agent_tools import ToolError
    from core.ignore_rules import BINARY_SUFFIXES, walk_files

    if root is None:
        raise ToolError("Проект не открыт.")
    if not isinstance(queries, (list, type(None))):
        raise ToolError("queries должен быть массивом строк.")
    terms = ([query] if query else []) + (queries or [])
    if (not terms or len(terms) > 24
            or any(not isinstance(term, str) or not term for term in terms)):
        raise ToolError("Укажи query или queries (до 24 непустых строк).")
    if type(context_lines) is not int or not 0 <= context_lines <= 10:
        raise ToolError("context_lines должен быть от 0 до 10.")
    if type(max_results) is not int or not 1 <= max_results <= 500:
        raise ToolError("max_results должен быть от 1 до 500.")
    try:
        matchers = [re.compile(term, re.IGNORECASE) for term in terms] if regex else []
    except re.error as exc:
        raise ToolError(f"Некорректное регулярное выражение: {exc}") from None
    root = Path(root).resolve()
    pattern = glob.replace("\\", "/")
    extension = pattern if re.fullmatch(r"\.\w+", pattern) else None
    truncated_scan = False
    if pattern and not extension and not any(char in pattern for char in "*?["):
        # An exact path should never enumerate thousands of unrelated files.
        candidate = (root / pattern).resolve()
        if not candidate.is_relative_to(root):
            raise ToolError("Поиск ограничен открытым проектом.")
        files = [candidate] if candidate.is_file() else []
    else:
        scan = walk_files(root, extensions={extension} if extension else None,
                          cancelled=should_stop)
        files = scan.files
        truncated_scan = scan.truncated
    folded = [term.casefold() for term in terms]
    if should_stop and should_stop():
        raise ToolError("Запрос остановлен.")
    output = []
    total = 0
    characters = 0
    limited = False
    for path in files:
        if should_stop and should_stop():
            raise ToolError("Запрос остановлен.")
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if pattern and not extension and not path.match(pattern):
            continue
        try:
            with path.open("rb") as source:
                data = source.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024 or b"\x00" in data:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = []
        for index, line in enumerate(lines):
            if should_stop and should_stop():
                raise ToolError("Запрос остановлен.")
            folded_line = line.casefold() if not regex else ""
            if (any(m.search(line) for m in matchers) if regex else
                    any(term in folded_line for term in folded)):
                hits.append(index)
                total += 1
                if total >= max_results:
                    limited = True
                    break
        hit_set = set(hits)
        # Merge adjacent ranges so overlapping context is sent only once.
        indexes = sorted({i for hit in hits for i in range(max(0, hit-context_lines),
                                                          min(len(lines), hit+context_lines+1))})
        rel = path.relative_to(root).as_posix()
        for index in indexes:
            separator = ":" if index in hit_set else "-"
            line = lines[index]
            excerpt = line[:400] + (" … [строка сокращена]" if len(line) > 400 else "")
            rendered = f"{rel}{separator}{index+1}{separator} {excerpt}"
            if characters + len(rendered) > 24_000:
                limited = True
                break
            output.append(rendered)
            characters += len(rendered) + 1
        if limited:
            break
    if limited or truncated_scan:
        output.append("[Поиск ограничен по объёму; уточни query/glob или увеличь max_results.]")
    return "\n".join(output) if output else "Совпадений не найдено."
