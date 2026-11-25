"""Bounded local search: one model call can locate several related symbols."""
from pathlib import Path
import json
import re
from time import monotonic


REGEX_MATCH_SECONDS = 0.05
REGEX_SEARCH_SECONDS = 3.0


def search_project(root, *, query="", queries=None, glob="", regex=False,
                   context_lines=2, max_results=40, should_stop=None, _stdlib_regex=False,
                   case_sensitive=False, output_mode="content"):
    # Imported lazily because the toolbox routes searches here.
    from services.agent_tools import ToolError
    from services.workbench_documents import DOCUMENT_SUFFIXES
    from core.ignore_rules import BINARY_SUFFIXES, walk_files

    if root is None:
        raise ToolError("Проект не открыт.")
    if type(case_sensitive) is not bool or output_mode not in {"content", "files", "count"}:
        raise ToolError("Укажи case_sensitive=true/false и output_mode=content/files/count.")
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
    matchers = []
    if regex:
        if _stdlib_regex:
            engine = re
        else:
            try:
                import regex as engine
            except ImportError:
                # Python's built-in engine can block in a single match. Run it
                # in a process we can stop even during that match.
                try:
                    for term in terms:
                        re.compile(term, 0 if case_sensitive else re.IGNORECASE)
                except re.error as exc:
                    raise ToolError(f"Некорректное регулярное выражение: {exc}") from None
                return _isolated_regex_search(root, {
                    "queries": terms, "glob": glob, "regex": True,
                    "context_lines": context_lines, "max_results": max_results,
                    "case_sensitive": case_sensitive, "output_mode": output_mode,
                }, should_stop)
        try:
            matchers = [engine.compile(term, 0 if case_sensitive else engine.IGNORECASE) for term in terms]
        except engine.error as exc:
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
                          cancelled=should_stop, max_entries=40_000)
        files = scan.files
        truncated_scan = scan.truncated
    folded = [term if case_sensitive else term.casefold() for term in terms]
    if should_stop and should_stop():
        raise ToolError("Запрос остановлен.")
    output = []
    total = 0
    characters = 0
    limited = False
    scanned_bytes = 0
    deadline = monotonic() + REGEX_SEARCH_SECONDS
    for path in files:
        if should_stop and should_stop():
            raise ToolError("Запрос остановлен.")
        if path.suffix.lower() in BINARY_SUFFIXES or path.suffix.lower() in DOCUMENT_SUFFIXES:
            continue
        if pattern and not extension and not _matches(path.relative_to(root), pattern):
            continue
        if scanned_bytes >= 32 * 1024 * 1024:
            limited = True
            break
        try:
            with path.open("rb") as source:
                data = source.read(4 * 1024 * 1024 + 1)
            scanned_bytes += len(data)
            if len(data) > 4 * 1024 * 1024 or b"\x00" in data:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = []
        for index, line in enumerate(lines):
            if should_stop and should_stop():
                raise ToolError("Запрос остановлен.")
            if matchers:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    limited = True
                    break
                try:
                    matched = (any(m.search(line) for m in matchers) if _stdlib_regex else
                               any(m.search(line, timeout=min(remaining, REGEX_MATCH_SECONDS), concurrent=True)
                                   for m in matchers))
                except TimeoutError:
                    raise ToolError("Регулярное выражение выполняется слишком долго. Уточни шаблон или glob.") from None
            else:
                folded_line = line if case_sensitive else line.casefold()
                matched = any(term in folded_line for term in folded)
            if matched:
                hits.append(index)
                total += 1
                if output_mode == "files":
                    break
                if total >= max_results:
                    limited = True
                    break
        hit_set = set(hits)
        if output_mode in {"files", "count"}:
            if hits:
                rel = path.relative_to(root).as_posix()
                output.append(rel if output_mode == "files" else f"{rel}: {len(hits)}")
            if limited or total >= max_results:
                limited = True
                break
            continue
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


def _matches(path, pattern):
    # **/ should match the project root as well as nested directories.
    return path.match(pattern) or (pattern.startswith("**/") and path.match(pattern[3:]))


def find_project_files(root, *, pattern="*", offset=0, limit=100, should_stop=None):
    from services.agent_tools import ToolError
    from core.ignore_rules import walk_files

    if root is None:
        raise ToolError("Проект не открыт.")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
        raise ToolError("Нужен непустой pattern длиной до 500 символов.")
    if type(offset) is not int or not 0 <= offset <= 40000 or type(limit) is not int or not 1 <= limit <= 500:
        raise ToolError("offset должен быть от 0 до 40000, limit — от 1 до 500.")
    root = Path(root).resolve()
    pattern = pattern.replace("\\", "/")
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ToolError("Поиск ограничен открытым проектом; используй относительный glob.")
    if should_stop and should_stop():
        raise ToolError("Запрос остановлен.")
    scan = walk_files(root, max_files=None, max_entries=40000, max_bytes=None, cancelled=should_stop)
    if should_stop and should_stop():
        raise ToolError("Запрос остановлен.")
    paths = sorted(path.relative_to(root).as_posix() for path in scan.files
                   if _matches(path.relative_to(root), pattern))
    end = offset + limit
    return json.dumps({"files": paths[offset:end], "next_offset": end if end < len(paths) else None,
                       "scan_truncated": scan.truncated}, ensure_ascii=False)


def _regex_worker(connection, root, options):
    with connection:
        try:
            result = search_project(root, **options, _stdlib_regex=True)
        except Exception as exc:
            connection.send((False, str(exc)))
        else:
            connection.send((True, result))


def _isolated_regex_search(root, options, should_stop):
    from multiprocessing import get_context

    from services.agent_tools import ToolError

    if should_stop and should_stop():
        raise ToolError("Запрос остановлен.")
    context = get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_regex_worker, args=(sender, root, options), daemon=True)
    with receiver, sender:
        worker.start()
        sender.close()
        try:
            deadline = monotonic() + REGEX_SEARCH_SECONDS + 2
            while monotonic() < deadline:
                if should_stop and should_stop():
                    raise ToolError("Запрос остановлен.")
                if receiver.poll(0.1):
                    try:
                        ok, value = receiver.recv()
                    except EOFError:
                        raise ToolError("Процесс поиска по регулярному выражению завершился с ошибкой.") from None
                    if not ok:
                        raise ToolError(value)
                    return value
            raise ToolError("Регулярное выражение выполняется слишком долго. Уточни шаблон или glob.")
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join()
            worker.close()
