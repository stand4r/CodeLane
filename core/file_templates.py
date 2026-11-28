"""File templates («Шаблоны новых файлов»).

При создании файла CodeLane предлагает готовые заготовки: Python-модуль,
Qt-виджет, тест pytest и README. Шаблоны — обычные JSON-файлы в
``.codelane/templates/`` проекта, поэтому их можно править вручную под стиль
проекта; при первом обращении недостающие шаблоны создаются автоматически.

Подстановки записываются как ``${name}`` — фигурные скобки Python-кода внутри
шаблона остаются нетронутыми.

No Qt imports here on purpose: the store is plain Python and can be used by the
explorer, the editor and tests alike.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.logging_config import get_logger
from core.project_state import PROJECT_FOLDER
from core.subprocess_utils import run_hidden

logger = get_logger("file_templates")

TEMPLATES_DIR = "templates"
TEMPLATE_SUFFIX = ".json"
PLACEHOLDER_PATTERN = re.compile(r"\$\{(\w+)\}")
EMPTY_TEMPLATE_ID = ""

#: Идентификаторы шаблонов, которые обязана содержать поставка.
DEFAULT_TEMPLATE_IDS = (
    "python-module",
    "qt-widget",
    "pytest-test",
    "readme",
)


@dataclass(frozen=True)
class FileTemplate:
    """Одна заготовка нового файла."""

    id: str
    title: str
    description: str = ""
    extension: str = ""
    body: str = ""

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "extension": self.extension,
            "body": self.body,
        }

    @classmethod
    def from_payload(cls, payload) -> FileTemplate | None:
        if not isinstance(payload, dict):
            return None
        template_id = str(payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not template_id or not title:
            return None
        body = payload.get("body")
        return cls(
            id=template_id,
            title=title,
            description=str(payload.get("description") or ""),
            extension=str(payload.get("extension") or ""),
            body=body if isinstance(body, str) else "",
        )


_DEFAULT_BODIES = {
    "python-module": (
        '"""${project}: ${module} — модуль проекта.\n'
        "\n"
        "Создан: ${date}\n"
        "Автор: ${author}\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        '__all__: list[str] = []\n'
        "\n"
        "\n"
        "def run() -> str:\n"
        '    """Точка входа модуля ${module}."""\n'
        '    return "${module}"\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    print(run())\n"
    ),
    "qt-widget": (
        '"""${project}: ${class_name} — виджет PyQt6.\n'
        "\n"
        "Создан: ${date}\n"
        "Автор: ${author}\n"
        '"""\n'
        "\n"
        "from PyQt6 import QtCore, QtWidgets\n"
        "\n"
        "\n"
        "class ${class_name}(QtWidgets.QWidget):\n"
        '    """Виджет ${class_name}."""\n'
        "\n"
        "    def __init__(self, parent=None):\n"
        "        super().__init__(parent)\n"
        '        self.setObjectName("${module}")\n'
        "        self._build_ui()\n"
        "\n"
        "    def _build_ui(self) -> None:\n"
        "        layout = QtWidgets.QVBoxLayout(self)\n"
        "        layout.setContentsMargins(12, 12, 12, 12)\n"
        "        layout.setSpacing(8)\n"
        "        self.label = QtWidgets.QLabel(self._title(), self)\n"
        "        layout.addWidget(self.label)\n"
        "\n"
        "    def _title(self) -> str:\n"
        '        return "${class_name}"\n'
    ),
    "pytest-test": (
        '"""Тесты ${module} — создано ${date} (${author})."""\n'
        "\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "ROOT = Path(__file__).resolve().parent.parent\n"
        "if str(ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT))\n"
        "\n"
        "\n"
        "def test_${subject}_smoke():\n"
        '    """Базовый каркас: замените на реальные проверки."""\n'
        "    assert True\n"
    ),
    "readme": (
        "# ${name}\n"
        "\n"
        "Краткое описание проекта «${project}».\n"
        "\n"
        "## Установка\n"
        "\n"
        "```bash\n"
        "pip install -r requirements.txt\n"
        "```\n"
        "\n"
        "## Запуск\n"
        "\n"
        "```bash\n"
        "python ${file_name}\n"
        "```\n"
        "\n"
        "---\n"
        "\n"
        "Создано: ${date} · Автор: ${author}\n"
    ),
}


def default_templates() -> list[FileTemplate]:
    """Встроенные шаблоны, которые CodeLane всегда умеет предложить."""
    return [
        FileTemplate(
            id="python-module",
            title="Python-модуль",
            description="Модуль с docstring в стиле проекта, __all__ и точкой входа.",
            extension=".py",
            body=_DEFAULT_BODIES["python-module"],
        ),
        FileTemplate(
            id="qt-widget",
            title="Qt-виджет",
            description="QWidget с базовой раскладкой и docstring по стилю проекта.",
            extension=".py",
            body=_DEFAULT_BODIES["qt-widget"],
        ),
        FileTemplate(
            id="pytest-test",
            title="Тест pytest",
            description="Файл теста с sys.path-хаком проекта и каркасом проверки.",
            extension=".py",
            body=_DEFAULT_BODIES["pytest-test"],
        ),
        FileTemplate(
            id="readme",
            title="README",
            description="Markdown-описание проекта: установка, запуск, авторство.",
            extension=".md",
            body=_DEFAULT_BODIES["readme"],
        ),
    ]


def templates_directory(root) -> Path:
    """Путь к каталогу шаблонов проекта."""
    return Path(root) / PROJECT_FOLDER / TEMPLATES_DIR


def _template_file(root, template_id: str) -> Path:
    safe_id = re.sub(r"[^\w.-]+", "-", template_id).strip("-") or "template"
    return templates_directory(root) / f"{safe_id}{TEMPLATE_SUFFIX}"


def ensure_templates(root) -> Path:
    """Создаёт каталог шаблонов и записывает недостающие встроенные шаблоны.

    Существующие файлы не перезаписываются: пользователь может править их под
    свой стиль. Ошибки записи логируются и не мешают созданию файла.
    """
    directory = templates_directory(root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Не удалось создать каталог шаблонов %s", directory, exc_info=True)
        return directory

    for template in default_templates():
        target = _template_file(root, template.id)
        if target.exists():
            continue
        try:
            target.write_text(
                json.dumps(template.to_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Не удалось записать шаблон %s", target, exc_info=True)
    return directory


def load_templates(root=None) -> list[FileTemplate]:
    """Шаблоны проекта из ``.codelane/templates/`` плюс недостающие встроенные.

    Без открытого проекта (``root is None``) возвращаются только встроенные
    шаблоны — на диске при этом ничего не создаётся.
    """
    if not root:
        return default_templates()

    ensure_templates(root)
    loaded: dict[str, FileTemplate] = {}
    directory = templates_directory(root)
    try:
        files = sorted(directory.glob(f"*{TEMPLATE_SUFFIX}"))
    except OSError:
        files = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Не удалось прочитать шаблон %s", path, exc_info=True)
            continue
        template = FileTemplate.from_payload(payload)
        if template is None:
            logger.warning("Шаблон %s пропущен: нет id или title", path)
            continue
        loaded.setdefault(template.id, template)

    defaults = default_templates()
    for template in defaults:
        loaded.setdefault(template.id, template)
    # Сначала встроенные шаблоны в привычном порядке, затем пользовательские.
    order = {template.id: index for index, template in enumerate(defaults)}
    return sorted(
        loaded.values(),
        key=lambda template: (order.get(template.id, len(order)), template.title.lower()),
    )


def detect_author(root=None) -> str:
    """Автор для подстановки: git user.name, затем имя пользователя ОС."""
    if root:
        try:
            result = run_hidden(
                ["git", "-C", str(root), "config", "user.name"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            name = (result.stdout or "").strip()
            if result.returncode == 0 and name:
                return name
        except (OSError, subprocess.SubprocessError):
            logger.debug("git user.name недоступен", exc_info=True)
    for variable in ("GIT_AUTHOR_NAME", "USERNAME", "USER", "LOGNAME"):
        value = (os.environ.get(variable) or "").strip()
        if value:
            return value
    return "автор"


def _split_words(name: str) -> list[str]:
    cleaned = re.sub(r"[^\w]+", "_", name, flags=re.UNICODE).strip("_")
    return [part for part in re.split(r"[_\W]+", cleaned) if part]


def placeholder_values(file_path, *, root=None, author=None, today=None) -> dict[str, str]:
    """Значения подстановок для конкретного создаваемого файла."""
    path = Path(file_path)
    stem = path.stem or path.name or "new_file"
    words = _split_words(stem) or ["new", "file"]
    module = "_".join(word.lower() for word in words)
    class_name = "".join(word[:1].upper() + word[1:].lower() for word in words) or "NewFile"
    current_date = today or date.today()
    project_root = Path(root) if root else path.parent
    try:
        relative = path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        relative = path.name
    return {
        "name": stem,
        "file_name": path.name or stem,
        "module": module,
        "subject": re.sub(r"^(test_|spec_)", "", module) or module,
        "class_name": class_name,
        "package": path.parent.name,
        "relative_path": relative,
        "project": project_root.name or "project",
        "date": current_date.strftime("%d.%m.%Y"),
        "iso_date": current_date.isoformat(),
        "year": str(current_date.year),
        "author": author or detect_author(root),
    }


def render_template(
    template: FileTemplate | None,
    file_path,
    *,
    root=None,
    author=None,
    today=None,
) -> str:
    """Текст нового файла по шаблону с подстановкой имени, даты и автора."""
    if template is None:
        return ""
    values = placeholder_values(file_path, root=root, author=author, today=today)

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    return PLACEHOLDER_PATTERN.sub(replace, template.body)


def template_for_path(templates, file_path) -> FileTemplate | None:
    """Шаблон, который стоит предложить по умолчанию для этого пути."""
    path = Path(file_path)
    name = path.name.lower()
    by_id = {template.id: template for template in templates}
    if name.startswith("test_") and name.endswith(".py"):
        return by_id.get("pytest-test")
    if name.endswith(".py"):
        return by_id.get("python-module")
    if name.endswith((".md", ".markdown")):
        return by_id.get("readme")
    return None
