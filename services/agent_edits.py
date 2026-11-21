"""Compose exact fragment edits against a project file snapshot."""

from dataclasses import dataclass
from difflib import unified_diff


EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "propose_edit",
        "description": "Предложить замену уникального фрагмента существующего файла. Правка сохраняется в памяти.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        }, "required": ["path", "old_text", "new_text"]},
    },
}


@dataclass(frozen=True)
class FileEdit:
    path: str
    before: str
    after: str
    agents: tuple[int, ...] = ()

    def diff(self):
        lines = unified_diff(self.before.splitlines(True), self.after.splitlines(True),
                             fromfile="a/" + self.path, tofile="b/" + self.path)
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in lines)

    def apply(self, toolbox):
        return toolbox.edit_file(self.path, self.before, self.after, expected_source=self.before)


def validate_fragment(args):
    fields = tuple(args.get(name) for name in ("path", "old_text", "new_text"))
    if not all(isinstance(value, str) for value in fields):
        raise ValueError("Укажи путь, исходный и новый текст.")
    path, old, new = fields
    if not path.strip() or not old or old == new:
        raise ValueError("Нужна изменённая замена уникального фрагмента.")
    return path, old, new


class EditCandidate:
    def __init__(self, toolbox):
        self.toolbox = toolbox
        self.sources = {}
        self.ranges = {}
        self.authors = {}
        self.count = 0

    def add(self, args, *, reconcile=False, agents=()):
        path, old, new = validate_fragment(args)
        path, source = self.toolbox.preview_edit_source(path)
        if self.sources.get(path, source) != source:
            raise ValueError("Файл изменился во время подготовки кандидата.")
        start = source.find(old)
        if start < 0 or source.find(old, start + 1) >= 0:
            raise ValueError("Исходный фрагмент должен встречаться ровно один раз.")
        end = start + len(old)
        ranges = self.ranges.setdefault(path, [])
        authors = self.authors.setdefault(path, {})
        if (start, end, new) in ranges:
            authors[(start, end)].update(agents)
            return "Такая правка уже есть в кандидате."
        overlaps = [part for part in ranges if start < part[1] and part[0] < end]
        if overlaps and (not reconcile or any(a < start or b > end for a, b, _ in overlaps)):
            raise ValueError("Правка пересекается с уже принятым фрагментом. Согласуй изменения.")
        contributors = set(agents)
        for part in overlaps:
            ranges.remove(part)
            contributors.update(authors.pop(part[:2]))
        authors[(start, end)] = contributors
        self.count -= len(overlaps)
        self.sources[path] = source
        ranges.append((start, end, new))
        self.count += 1
        return f"Правка {path} добавлена в общий кандидат."

    def files(self):
        result = []
        for path, source in self.sources.items():
            changed = source
            for start, end, new in sorted(self.ranges[path], reverse=True):
                changed = changed[:start] + new + changed[end:]
            if changed != source:
                contributors = set().union(*self.authors[path].values())
                result.append(FileEdit(path, source, changed, tuple(sorted(contributors))))
        return result
