"""Project-local CodeLane configuration and legacy state migration."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from core.logging_config import get_logger

PROJECT_FOLDER = ".codelane"
logger = get_logger("project_state")


def write_json(target: Path, payload) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", dir=target.parent, encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            json.dump(payload, file, ensure_ascii=False, indent=2)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def project_state_file(root, name: str, *, legacy=None, default=None) -> Path:
    """Use project-local state, seeding it once from the previous location."""
    root = Path(root)
    target = root / PROJECT_FOLDER / name
    previous = Path(legacy) if legacy is not None else root / ".pyscribe" / name
    if target.exists():
        return target
    try:
        payload = json.loads(previous.read_text(encoding="utf-8")) if previous.is_file() else default
    except (OSError, ValueError):
        logger.warning("Не удалось импортировать %s", previous, exc_info=True)
        payload = default
    try:
        if payload is not None:
            write_json(target, payload)
    except OSError:
        logger.warning("Не удалось подготовить %s", target, exc_info=True)
        if legacy is not None or previous.is_file():
            return previous
    return target


def sync_project_config(config, root: str, *, save: bool = False) -> None:
    """Load portable project options or persist edited options."""
    options = {"name": Path(root).name, "description": "", "launch_command": "",
               "analyzers": ["structure", "dependencies"]}
    for key in options:
        value = config.get(key) if isinstance(config, dict) else getattr(config, key, None)
        if _valid_option(key, value):
            options[key] = value
    if save:
        write_json(project_state_file(root, "project.json"), options)
        return
    target = project_state_file(root, "project.json", default=options)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in options:
                value = data.get(key)
                if _valid_option(key, value):
                    if isinstance(config, dict):
                        config[key] = value
                    else:
                        setattr(config, key, value)
    except (OSError, ValueError):
        logger.warning("Не удалось обработать %s", target, exc_info=True)


def _valid_option(key, value):
    if key == "analyzers":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, str)
