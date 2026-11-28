"""Parse unified diffs and build patches for partial staging.

The source control panel reviews ``git diff`` output hunk by hunk and stages
only the selected hunks with ``git apply --cached``.  Parsing and patch
building live here, without Qt, so they are easy to test.
"""

from dataclasses import dataclass, field
import re
import shlex


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass(frozen=True)
class DiffHunk:
    """One ``@@ … @@`` block with its raw body lines."""

    header: str
    lines: tuple[str, ...]

    @property
    def added(self):
        return sum(1 for line in self.lines if line.startswith("+") and not line.startswith("+++"))

    @property
    def removed(self):
        return sum(1 for line in self.lines if line.startswith("-") and not line.startswith("---"))

    @property
    def old_start(self):
        match = HUNK_RE.match(self.header)
        return int(match.group(1)) if match else 0

    @property
    def new_start(self):
        match = HUNK_RE.match(self.header)
        return int(match.group(3)) if match else 0

    def summary(self):
        match = HUNK_RE.match(self.header)
        section = match.group(5).strip() if match else ""
        head = f"@@ {self.old_start} → {self.new_start} @@"
        if section:
            head = f"{head} {section}"
        return f"{head}   +{self.added} −{self.removed}"

    def text(self):
        body = "".join(line + "\n" for line in self.lines)
        return self.header + "\n" + body


@dataclass
class DiffFile:
    """One file section of a unified diff."""

    header: list = field(default_factory=list)
    old_path: str = ""
    new_path: str = ""
    hunks: list = field(default_factory=list)

    @property
    def path(self):
        """Path as it exists after the change (``/dev/null`` for deletions)."""
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        return self.old_path

    @property
    def deleted(self):
        return self.new_path == "/dev/null"

    def added(self):
        return sum(hunk.added for hunk in self.hunks)

    def removed(self):
        return sum(hunk.removed for hunk in self.hunks)

    def summary(self):
        return f"+{self.added()} −{self.removed()}"


def _clean_path(line):
    path = line[4:].strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path if path == "/dev/null" else _strip_prefix(path)


def _strip_prefix(path):
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _git_line_paths(line):
    """``(old, new)`` paths from a ``diff --git`` header, quotes included."""
    rest = line[len("diff --git "):]
    try:
        parts = shlex.split(rest)
    except ValueError:
        parts = rest.split(" ")
    if len(parts) < 2:
        return "", ""
    return _strip_prefix(parts[0]), _strip_prefix(parts[1])


def parse_diff(text):
    """Split unified diff text into :class:`DiffFile` objects with hunks."""
    files = []
    current = None
    hunk_header = None
    hunk_lines = []

    def flush_hunk():
        nonlocal hunk_header, hunk_lines
        if hunk_header is not None and current is not None:
            current.hunks.append(DiffHunk(hunk_header, tuple(hunk_lines)))
        hunk_header = None
        hunk_lines = []

    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            flush_hunk()
            current = DiffFile(header=[line])
            files.append(current)
            current.old_path, current.new_path = _git_line_paths(line)
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            flush_hunk()
            if HUNK_RE.match(line):
                hunk_header = line
            continue
        if hunk_header is not None:
            hunk_lines.append(line)
            continue
        current.header.append(line)
        if line.startswith("--- "):
            current.old_path = _clean_path(line)
        elif line.startswith("+++ "):
            current.new_path = _clean_path(line)
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to "):].strip()
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from "):].strip()
    flush_hunk()
    return files


def build_patch(file, indexes):
    """Patch text with only the selected hunk indexes of ``file``."""
    selected = [file.hunks[index] for index in indexes if 0 <= index < len(file.hunks)]
    if not selected:
        return ""
    header = "".join(line + "\n" for line in file.header)
    if not any(line.startswith("--- ") for line in file.header):
        old = "/dev/null" if not file.old_path else "a/" + file.old_path
        new = "/dev/null" if file.deleted else "b/" + file.new_path
        header += f"--- {old}\n+++ {new}\n"
    return header + "".join(hunk.text() for hunk in selected)


def split_hunks(diff_text):
    """All hunks of a single-file diff as ``(path, hunk)`` pairs."""
    return [(file.path, hunk) for file in parse_diff(diff_text) for hunk in file.hunks]


def diff_stats(text):
    """``(added, removed)`` line counts of unified diff text, headers excluded."""
    added = removed = 0
    for line in (text or "").splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


SHORTSTAT_RE = re.compile(r"(\d+) files? changed")
INSERTIONS_RE = re.compile(r"(\d+) insertions?\(\+\)")
DELETIONS_RE = re.compile(r"(\d+) deletions?\(-\)")


def parse_shortstat(text):
    """``(files, added, removed)`` from ``git diff --shortstat`` output."""
    text = text or ""
    files = SHORTSTAT_RE.search(text)
    added = INSERTIONS_RE.search(text)
    removed = DELETIONS_RE.search(text)
    return (int(files.group(1)) if files else 0,
            int(added.group(1)) if added else 0,
            int(removed.group(1)) if removed else 0)
