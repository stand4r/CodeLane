"""Context-checked multi-file patches, using the same editor buffers as file tools."""
import json

from core.file_snapshot import snapshot_file, write_if_unchanged


def parse_patch(patch):
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("Патч должен быть текстом размером до 8 МБ.")
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Нужны маркеры *** Begin Patch и *** End Patch.")
    entries, index = [], 1
    while index < len(lines) - 1:
        header = lines[index]
        operation = next((name for name in ("Add", "Update", "Delete")
                          if header.startswith(f"*** {name} File: ")), None)
        if operation is None:
            raise ValueError(f"Ожидался заголовок файла: {header[:120]}")
        path = header.split(": ", 1)[1].strip()
        if not path:
            raise ValueError("Пустой путь файла.")
        index += 1
        move = None
        if operation == "Update" and lines[index].startswith("*** Move to: "):
            move = lines[index].split(": ", 1)[1].strip()
            index += 1
        body = []
        while index < len(lines) - 1 and not lines[index].startswith(("*** Add File:", "*** Update File:", "*** Delete File:")):
            body.append(lines[index])
            index += 1
        if operation == "Add" and any(not line.startswith("+") for line in body):
            raise ValueError("Строки нового файла должны начинаться с +.")
        if operation == "Delete" and body:
            raise ValueError("Удаление файла не принимает фрагменты.")
        if operation == "Update" and not body and not move:
            raise ValueError("Обновление требует фрагмента или пути переноса.")
        entries.append({"operation": operation, "path": path, "move": move, "body": body})
    if not 1 <= len(entries) <= 40:
        raise ValueError("Патч должен содержать от 1 до 40 файлов.")
    return entries


def update_text(before, body):
    """Find each hunk exactly once in order; never guess across ambiguous contexts."""
    source = before.splitlines(keepends=True)
    source = [line[:-1] if line.endswith("\n") else line for line in source]
    result, cursor, index = [], 0, 0
    while index < len(body):
        marker = body[index]
        if not marker.startswith("@@"):
            raise ValueError("Каждый фрагмент обновления должен начинаться с @@.")
        anchor = marker[2:].strip()
        index += 1
        if anchor:
            anchors = [i for i in range(cursor, len(source)) if source[i] == anchor]
            if len(anchors) != 1:
                raise ValueError("Контекст после @@ отсутствует или неоднозначен.")
            end = anchors[0] + 1
            result.extend(source[cursor:end])
            cursor = end
        old, new, eof = [], [], False
        while index < len(body) and not body[index].startswith("@@"):
            line = body[index]
            index += 1
            if line == "*** End of File":
                eof = True
                if index < len(body):
                    raise ValueError("Маркер конца файла должен быть последним.")
                break
            if not line or line[0] not in " +-":
                raise ValueError("Строки фрагмента должны начинаться с пробела, + или -.")
            if line[0] in " -":
                old.append(line[1:])
            if line[0] in " +":
                new.append(line[1:])
        if not old:
            if source and not eof and not anchor:
                raise ValueError("Для вставки укажи окружающий контекст или *** End of File.")
            positions = [len(source) if eof else cursor]
        else:
            positions = [i for i in range(cursor, len(source) - len(old) + 1)
                         if source[i:i + len(old)] == old and (not eof or i + len(old) == len(source))]
        if len(positions) != 1:
            raise ValueError("Фрагмент отсутствует или встречается несколько раз. Уточни контекст патча.")
        start = positions[0]
        result.extend(source[cursor:start])
        result.extend(new)
        cursor = start + len(old)
    if not body:
        return before
    result.extend(source[cursor:])
    return "\n".join(result) + ("\n" if result and (before.endswith("\n") or not before) else "")


def apply_patch(toolbox, patch):
    from services.agent_tools import MAX_WRITE_BYTES, ToolError

    if not toolbox._allowed("apply_patch"):
        raise ToolError("Применение патчей запрещено настройками доступа.")
    try:
        entries = parse_patch(patch)
        prepared, paths = [], set()
        for entry in entries:
            toolbox._check_read_cancelled()
            target = toolbox._resolve(entry["path"])
            destination = toolbox._resolve(entry["move"]) if entry["move"] else target
            if target in paths or (destination != target and destination in paths):
                raise ValueError("Один путь не может участвовать в нескольких операциях патча.")
            paths.update((target, destination))
            operation = entry["operation"]
            buffer = "__NO_OPEN_EDITOR_BUFFER__"
            if toolbox.ui_action and not toolbox._outside_project(target):
                buffer = toolbox.ui_action("read_buffer", {"path": str(target)})
            if operation == "Add":
                if target.exists() or buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    raise ValueError(f"Новый файл уже существует: {entry['path']}")
                before, expected = "", None
                after = "\n".join(line[1:] for line in entry["body"])
                if entry["body"]:
                    after += "\n"
            else:
                _, data, expected, before = toolbox._edit_source(entry["path"])
                data.decode("utf-8")
                after = update_text(before, entry["body"]) if operation == "Update" else None
                if (operation == "Delete" or destination != target) and buffer != "__NO_OPEN_EDITOR_BUFFER__":
                    raise ValueError("Перед удалением или переносом сохрани и закрой открытую вкладку.")
            if destination != target and destination.exists():
                raise ValueError(f"Путь назначения уже существует: {entry['move']}")
            if after is not None and len(after.encode("utf-8")) > MAX_WRITE_BYTES:
                raise ValueError("Итоговый файл превышает лимит записи.")
            prepared.append((entry, target, destination, expected, before, after, buffer))
        toolbox._ask("apply_patch", patch, "Применить патч к файлам")
        # Validate the entire patch again after approval before the first mutation.
        for entry, target, destination, expected, before, after, buffer in prepared:
            toolbox._check_read_cancelled()
            if snapshot_file(target) != expected or (destination != target and destination.exists()):
                raise ValueError(f"Файл изменился после подготовки патча: {entry['path']}")
            if toolbox.ui_action and not toolbox._outside_project(target):
                if toolbox.ui_action("read_buffer", {"path": str(target)}) != buffer:
                    raise ValueError(f"Буфер изменился после подготовки патча: {entry['path']}")
    except (ValueError, OSError) as exc:
        raise ToolError(f"Патч не применён: {exc}") from exc
    changes = []
    try:
        for entry, target, destination, expected, before, after, buffer in prepared:
            toolbox._check_read_cancelled()
            if entry["operation"] == "Update" and target == destination and after == before:
                continue
            cache = getattr(toolbox, "team_cache", None)
            if (cache is not None and entry["operation"] == "Update" and target == destination
                    and not toolbox._outside_project(target)):
                from services.agent_edits import FileEdit
                cache.apply(str(toolbox.root), FileEdit(entry["path"], before, after), toolbox,
                            getattr(toolbox, "team_run_id", None))
            elif entry["operation"] == "Delete":
                if snapshot_file(target) != expected:
                    raise ToolError("Удаляемый файл изменился.")
                target.unlink()
            elif buffer != "__NO_OPEN_EDITOR_BUFFER__":
                toolbox.ui_action("replace_buffer", {"path": str(target), "expected": before, "content": after})
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                write_if_unchanged(destination, after.encode("utf-8"), expected if target == destination else None)
                if target != destination:
                    if snapshot_file(target) != expected:
                        changes.append({"path": entry["move"], "operation": "Add",
                                        "source": entry["path"], "unsaved": False})
                        raise ToolError(f"Файл изменился при переносе; копия создана: {destination}")
                    target.unlink()
            changes.append({"path": entry["move"] or entry["path"], "operation": entry["operation"],
                            "source": entry["path"], "unsaved": buffer != "__NO_OPEN_EDITOR_BUFFER__"})
    except Exception as exc:
        # Cross-file operations are preflighted, but not an OS-wide transaction.
        return json.dumps({"ok": False, "changes": changes, "error": str(exc)}, ensure_ascii=False)
    return json.dumps({"ok": True, "changes": changes}, ensure_ascii=False)
