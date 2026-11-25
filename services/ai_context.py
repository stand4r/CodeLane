"""Context assembly and caching for the PyScribe agent.

The agent pays for every character it sends, and the same project files are
attached again and again. This module keeps a small stat-cache of file contents
and of the project tree so repeated requests (and repeated @-mentions) reuse
what was already read, and re-reads only files that actually changed on disk.

The produced block is plain text placed after the user's message:

* project root and a compact file map (contents are fetched on demand);
* full contents of explicitly @-mentioned files.

Nothing here is Qt-specific.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional, Tuple

from services.ai_budget import clip_utf8

__all__ = [
    "ContextCache",
    "get_context_cache",
    "render_project_context",
    "context_cache_stats",
]

TREE_TTL_SEC = 20.0
TREE_LIMIT = 400
PROJECT_CONTEXT_BYTES = 48_000
# Explicit @-mentions are useful context, but must not let a single generated or
# accidentally selected file consume the entire request window.
MENTION_FILE_BYTES = 12_000
# Bound all attached mentions together, not just each individual file. The
# surrounding labels/context header are small; reserve a little room for them.
MENTIONS_TOTAL_BYTES = 32_000
MENTION_TRUNCATED = "\n… [файл усечён для экономии контекста; остальные строки доступны через read_file] …\n"


@dataclass
class ContextCache:
    """mtime/size-keyed cache of file contents and the project tree."""

    files: Dict[str, Tuple[int, int, str]] = field(default_factory=dict)
    trees: Dict[str, Tuple[float, List[str]]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def read(self, path: Path) -> str:
        """Return a preview, reusing the cached copy while the file is unchanged."""
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return ""
        cached = self.files.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            self.hits += 1
            return cached[2]
        try:
            with path.open("rb") as source:
                raw = source.read(MENTION_FILE_BYTES + 1)
                if len(raw) > MENTION_FILE_BYTES:
                    half = (MENTION_FILE_BYTES - len(MENTION_TRUNCATED.encode("utf-8"))) // 2
                    source.seek(-half, 2)
                    text = (raw[:half].decode("utf-8", errors="replace") + MENTION_TRUNCATED
                            + source.read(half).decode("utf-8", errors="replace"))
                else:
                    text = raw.decode("utf-8", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        except OSError:
            return ""
        with self._lock:
            if key not in self.files and len(self.files) >= 128:
                self.files.pop(next(iter(self.files)))
            self.files[key] = (stat.st_mtime_ns, stat.st_size, text)
        self.misses += 1
        return text

    def tree(self, root: Path, should_stop=None) -> List[str]:
        """Relative file list of the project, refreshed at most every TTL."""
        key = str(root)
        cached = self.trees.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < TREE_TTL_SEC:
            self.hits += 1
            return list(cached[1])
        from core.ignore_rules import walk_files
        try:
            scan = walk_files(root, extensions=None, max_files=TREE_LIMIT, max_entries=20_000,
                              max_bytes=4 * 1024 * 1024, cancelled=should_stop)
        except Exception:  # noqa: BLE001 - an unreadable tree must not break chat
            return []
        names: List[str] = []
        for path in scan.files:
            try:
                names.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
        if not (should_stop and should_stop()):
            self.trees[key] = (now, names)
        self.misses += 1
        return list(names)

    def invalidate(self, path: Optional[Path] = None) -> None:
        with self._lock:
            if path is None:
                self.files.clear()
                self.trees.clear()
            else:
                self.files.pop(str(path), None)

    def stats(self) -> Dict[str, int]:
        return {"hits": self.hits, "misses": self.misses,
                "files": len(self.files), "trees": len(self.trees)}


_CACHE = ContextCache()


def get_context_cache() -> ContextCache:
    return _CACHE


def context_cache_stats() -> Dict[str, int]:
    return _CACHE.stats()


def _tree_summary(names: Iterable[str]) -> str:
    """Compact tree: per-directory file counts instead of 400 flat lines."""
    counts: Dict[str, int] = {}
    top_files: List[str] = []
    for rel in names:
        if "/" in rel:
            directory = rel.rsplit("/", 1)[0]
            counts[directory] = counts.get(directory, 0) + 1
        else:
            top_files.append(rel)
    lines = []
    for directory in sorted(counts)[:120]:
        lines.append(f"{directory}/  ({counts[directory]})")
    if top_files:
        lines.append("")
        lines.extend(sorted(top_files)[:120])
    if len(names) >= TREE_LIMIT:
        lines.append(f"… (дерево ограничено первыми {TREE_LIMIT} файлами)")
    return "\n".join(lines)


def render_project_context(root: Optional[Path], mentions: Optional[List[str]] = None,
                           cache: Optional[ContextCache] = None, should_stop=None) -> str:
    """Build the project part of the context block (cached where possible)."""
    if root is None or (should_stop and should_stop()):
        return ""
    cache = cache or _CACHE
    parts: List[str] = []
    if mentions:
        mention_bytes = 0
        root = root.resolve()
        for rel in dict.fromkeys(mentions):
            if should_stop and should_stop():
                break
            if mention_bytes >= MENTIONS_TOTAL_BYTES:
                parts.append("Упоминания файлов ограничены общим бюджетом; остальные файлы не приложены.")
                break
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if not candidate.is_file():
                continue
            text = cache.read(candidate)
            if text:
                encoded = text.encode("utf-8")
                available = MENTIONS_TOTAL_BYTES - mention_bytes
                if available <= 0:
                    parts.append("Упоминания файлов ограничены общим бюджетом; остальные файлы не приложены.")
                    break
                prefix = f"Файл проекта {rel}:\n```\n"
                suffix = "\n```"
                file_budget = min(MENTION_FILE_BYTES, available - len((prefix + suffix).encode("utf-8")) - 2)
                marker_bytes = len(MENTION_TRUNCATED.encode("utf-8"))
                if file_budget <= marker_bytes:
                    parts.append("Упоминания файлов ограничены общим бюджетом; остальные файлы не приложены.")
                    break
                if len(encoded) > file_budget:
                    budget = file_budget - marker_bytes
                    head_size = budget // 2
                    tail_size = budget - head_size
                    head = encoded[:head_size].decode("utf-8", errors="ignore")
                    tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
                    text = head + MENTION_TRUNCATED + tail
                block = prefix + text + suffix
                parts.append(block)
                mention_bytes += len(block.encode("utf-8")) + 2
        if not parts:
            parts.append(clip_utf8(_tree_summary(cache.tree(root, should_stop)), 3600))
    else:
        names = cache.tree(root, should_stop)
        parts.append("Карта проекта (содержимое файлов не приложено):\n"
                     + clip_utf8("\n".join(names) if len(names) <= 32 else _tree_summary(names), 3600))
        parts.append("Для поиска фрагментов используй search_in_project(regex=true, glob, queries, context_lines). "
                     "Для точной правки дочитай только недостающие строки через read_file(offset, limit).")
        if len(names) >= TREE_LIMIT:
            parts.append("Карта ограничена; остальные пути доступны через list_directory и поиск.")
    if not parts:
        return ""
    header = f"Корень проекта: {root}"
    return "<context>\n" + header + "\n\n" + "\n\n".join(parts) + "\n</context>"

