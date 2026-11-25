"""Временные файлы, которыми пользуются инструменты ИИ-агента.

Агент нередко пишет вспомогательный скрипт (``tmp_check.py``), запускает его
командой и оставляет мусор в проекте. ``TempFiles`` запоминает временные файлы,
существовавшие до начала работы, и после завершения запроса удаляет только те,
что появились за это время: файлы пользователя с похожими именами остаются.

Правила намеренно узкие — удаляется только то, что агент создал сам и что
названо как черновик; каталоги, исключённые из сканирования проекта (``.git``,
``node_modules``, ``venv`` и т. п.), не обходятся.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Set

from core.ignore_rules import should_skip_dir

__all__ = ["TEMPORARY_NAME", "TempFiles", "is_temporary_name"]

# ``tmp_check.py``, ``temp_scan.ps1``, ``scratch.txt``, ``data.tmp``, ``~$report.docx``.
TEMPORARY_NAME = re.compile(
    r"^(?:tmp|temp|scratch|_tmp|_temp)[._\-\d]"            # черновой префикс
    r"|\.(?:tmp|temp|part|bak)$"                            # черновой суффикс
    r"|^~\$",                                               # служебный файл Office
    re.IGNORECASE,
)


def is_temporary_name(name: str) -> bool:
    """True для имён, которые агент использует под черновики и скрипты-обёртки."""
    return bool(name) and bool(TEMPORARY_NAME.search(Path(str(name)).name))


class TempFiles:
    """Снимок временных файлов проекта и удаление появившихся после снимка."""

    def __init__(self, root: Optional[str], enabled: bool = True):
        self.root = Path(root).resolve() if root else None
        self.enabled = bool(enabled and self.root is not None)
        self.before: Set[str] = self._scan() if self.enabled else set()
        self.removed: List[str] = []

    def _scan(self) -> Set[str]:
        """Пути временных файлов относительно корня (без обхода служебных каталогов)."""
        found: Set[str] = set()
        if not self.enabled or self.root is None:
            return found
        for directory, subdirs, files in os.walk(self.root):
            subdirs[:] = [name for name in subdirs if not should_skip_dir(name)]
            for name in files:
                if not is_temporary_name(name):
                    continue
                path = Path(directory) / name
                try:
                    found.add(path.relative_to(self.root).as_posix())
                except ValueError:
                    continue
        return found

    def cleanup(self) -> List[str]:
        """Удалить созданные за время запроса черновики; вернуть их относительные пути."""
        self.removed = []
        if not self.enabled or self.root is None:
            return self.removed
        created = self._scan() - self.before
        for relative in sorted(created):
            target = self.root / relative
            try:
                if target.is_file() and not target.is_symlink():
                    target.unlink()
                else:
                    continue
            except OSError:
                continue
            self.removed.append(relative)
        self.before = self._scan()
        return self.removed
