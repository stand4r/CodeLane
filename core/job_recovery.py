"""Восстановление отложенных заданий команды после аварийного закрытия CodeLane.

Тонкий Qt-слой над :class:`services.team_cache.TeamCache`: путь хранилища,
общий экземпляр кэша, восстановление «зависших» заданий и heartbeat-таймер,
который подтверждает, что экземпляр CodeLane жив.
"""
from pathlib import Path

from PyQt6 import QtCore

from core.logging_config import get_logger
from services.team_cache import TeamCache

logger = get_logger("job_recovery")

_CACHE = None


def cache_dir():
    """Каталог общего хранилища команды (тот же, что у ``AgentPlanner.cache_dir``)."""
    location = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(location) / "agent-team"


def cache():
    """Общий экземпляр :class:`TeamCache` для задач восстановления и heartbeat."""
    global _CACHE
    if _CACHE is None:
        _CACHE = TeamCache(cache_dir())
    return _CACHE


def recover_interrupted(project=None):
    """
    Приостановить задания, чей экземпляр CodeLane больше не жив.

    Args:
        project: ограничить восстановление одним проектом (None — все проекты).

    Returns:
        int: сколько заданий переведено в ``paused``.
    """
    return cache().recover(project=project)


def _beat():
    """Безопасный heartbeat: сбой записи не должен ломать GUI-таймер."""
    try:
        cache().heartbeat()
    except Exception:  # noqa: BLE001 - журнал диагностики вместо исключения в слоте Qt
        logger.exception("Не удалось записать heartbeat отложенных заданий")


def start_heartbeat(parent, interval_ms=15000):
    """
    Зарегистрировать экземпляр и подтверждать его жизнь по таймеру.

    Args:
        parent: QObject-владелец таймера (обычно ``QApplication``).
        interval_ms: период heartbeat в миллисекундах.

    Returns:
        QtCore.QTimer: запущенный таймер; останавливается вместе с ``parent``.
    """
    timer = QtCore.QTimer(parent)
    timer.setInterval(interval_ms)
    timer.timeout.connect(_beat)
    _beat()
    timer.start()
    return timer
