"""Регрессия восстановления отложенных заданий после аварийного закрытия CodeLane.

Стиль — как tests/test_team_cache.py: pytest + tmp_path, без Qt и без сети.
Контракт — docs/acceptance/delayed_jobs_recovery.md, публичный API
services/team_cache.py: recover(), claim(), state(..., reason=...),
heartbeat(), alive(), STALE_AFTER, REASON_INTERRUPTED.
Время внедряется через TeamCache(clock=...) — штатная точка подмены часов.
"""
import sqlite3
import time

import pytest

from services.team_cache import REASON_INTERRUPTED, STALE_AFTER, TeamCache


class Clock:
    """Управляемые часы: свежий экземпляр можно создать «позже по времени»."""

    def __init__(self, now=None):
        self.now = time.time() if now is None else now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def job(cache, project, ident):
    """Строка задания по id или падение с понятным текстом."""
    rows = [row for row in cache.jobs(project) if row["id"] == ident]
    assert rows, f"задание {ident} проекта {project} потерялось"
    return rows[0]


def interrupt(cache, project, ident, owner="dead-instance"):
    """Смоделировать сбой: задание захвачено, но владелец больше не подаёт признаков жизни."""
    assert cache.claim(project, ident)
    with cache.connect() as db:
        db.execute("UPDATE jobs SET owner=?, heartbeat=? WHERE id=? AND project=?",
                   (owner, cache.clock() - 10 * STALE_AFTER, ident, project))
    return owner


def pause(cache, project, ident, reason="Ждём готовности условия"):
    """Перевести задание в paused с причиной."""
    assert cache.claim(project, ident)
    assert cache.state(project, ident, "paused", "running", reason)
    return reason


def test_interrupted_job_becomes_paused_with_reason_and_deletes(tmp_path):
    directory, project = tmp_path / "cache", str(tmp_path)
    clock = Clock()
    dead = TeamCache(directory, owner="dead-instance", clock=clock)
    dead.save_job(project, "Проверка", "test", "Проверить граф", 0, "code.txt")
    ident = dead.jobs(project)[0]["id"]
    assert dead.claim(project, ident)
    dead.heartbeat()
    with dead.connect() as db:
        db.execute("UPDATE instances SET process_created=process_created-1 WHERE token=?", (dead.owner,))
    clock.advance(3600)  # экземпляр умер час назад

    fresh = TeamCache(directory, clock=clock)  # свежий экземпляр, часы сдвинуты вперёд
    assert fresh.recover() == 1

    row = job(fresh, project, ident)
    assert row["state"] == "paused"
    assert row["reason"] == REASON_INTERRUPTED
    assert row["reason"].strip() and "прерван" in row["reason"].lower()

    assert fresh.recover() == 0  # идемпотентно
    again = job(fresh, project, ident)
    assert (again["state"], again["reason"]) == ("paused", row["reason"])

    assert fresh.remove_job(project, ident) is True
    assert fresh.jobs(project) == []


def test_live_foreign_instance_keeps_running_and_blocks_delete(tmp_path):
    directory, project = tmp_path / "cache", str(tmp_path)
    clock = Clock()
    cache = TeamCache(directory, clock=clock)
    cache.save_job(project, "Чужая работа", "code", "Сделать", 0, "")
    ident = cache.jobs(project)[0]["id"]
    other = TeamCache(directory, owner="other-live", clock=clock)
    other.heartbeat()  # второй экземпляр жив
    with cache.connect() as db:
        db.execute("UPDATE jobs SET state='running',owner='other-live',heartbeat=? "
                   "WHERE id=? AND project=?", (clock(), ident, project))

    assert cache.recover() == 0
    row = job(cache, project, ident)
    assert (row["state"], row["owner"]) == ("running", "other-live")
    assert row["reason"] is None

    with pytest.raises(ValueError):
        cache.remove_job(project, ident)
    assert job(cache, project, ident)["state"] == "running"


def test_own_running_job_survives_recovery(tmp_path):
    project = str(tmp_path)
    clock = Clock()
    cache = TeamCache(tmp_path / "cache", clock=clock)
    cache.save_job(project, "Своё", "code", "Сделать", 0, "")
    ident = cache.jobs(project)[0]["id"]
    assert cache.claim(project, ident)

    clock.advance(10 * STALE_AFTER)  # часы уехали вперёд, heartbeat текущего экземпляра протух
    assert cache.recover() == 0
    row = job(cache, project, ident)
    assert row["state"] == "running"
    assert row["owner"] == cache.owner
    with pytest.raises(ValueError):
        cache.remove_job(project, ident)


def test_retry_clears_reason_and_claims_once(tmp_path):
    project = str(tmp_path)
    clock = Clock()
    cache = TeamCache(tmp_path / "cache", clock=clock)
    cache.save_job(project, "Повтор", "test", "Перепроверить", 3, "")
    ident = cache.jobs(project)[0]["id"]
    interrupt(cache, project, ident)
    assert cache.recover() == 1
    assert job(cache, project, ident)["state"] == "paused"

    assert cache.state(project, ident, "queued", "paused") is True
    row = job(cache, project, ident)
    assert row["state"] == "queued"
    assert row["reason"] is None
    assert row["owner"] is None
    assert (row["started_at"], row["heartbeat"]) == (None, None)

    assert cache.claim(project, ident) is True
    assert cache.state(project, ident, "running", "queued") is False  # второй захват — False
    row = job(cache, project, ident)
    assert row["state"] == "running"
    assert row["owner"] == cache.owner
    assert row["reason"] is None
    assert len(cache.jobs(project)) == 1
    assert (row["title"], row["role"], row["prompt"], row["due"]) == ("Повтор", "test", "Перепроверить", 3)


def test_delete_paused_and_recovered_but_not_foreign_project(tmp_path):
    project, foreign_project = str(tmp_path), str(tmp_path / "other")
    (tmp_path / "other").mkdir()
    clock = Clock()
    cache = TeamCache(tmp_path / "cache", clock=clock)

    cache.save_job(project, "Пауза", "code", "Сделать", 0, "")
    paused = cache.jobs(project)[0]["id"]
    pause(cache, project, paused)
    assert cache.remove_job(project, paused) is True

    cache.save_job(project, "Прервано", "code", "Сделать", 0, "")
    broken = cache.jobs(project)[0]["id"]
    interrupt(cache, project, broken)

    cache.save_job(foreign_project, "Чужое", "code", "Сделать", 0, "")
    foreign = cache.jobs(foreign_project)[0]["id"]
    interrupt(cache, foreign_project, foreign)

    assert cache.recover(project=project) == 1
    assert job(cache, project, broken)["state"] == "paused"
    assert cache.remove_job(project, broken) is True
    assert cache.jobs(project) == []

    assert cache.remove_job(project, foreign) is False
    row = job(cache, foreign_project, foreign)
    assert (row["state"], row["owner"]) == ("running", "dead-instance")


def test_recover_is_project_scoped_then_global(tmp_path):
    first, second = str(tmp_path), str(tmp_path / "second")
    (tmp_path / "second").mkdir()
    cache = TeamCache(tmp_path / "cache", clock=Clock())
    cache.save_job(first, "Первое", "code", "Сделать", 0, "")
    cache.save_job(second, "Второе", "code", "Сделать", 0, "")
    job_first = cache.jobs(first)[0]["id"]
    job_second = cache.jobs(second)[0]["id"]
    interrupt(cache, first, job_first)
    interrupt(cache, second, job_second)

    assert cache.recover(project=first) == 1
    assert job(cache, first, job_first)["state"] == "paused"
    assert job(cache, second, job_second)["state"] == "running"

    assert cache.recover() == 1
    assert job(cache, second, job_second)["state"] == "paused"
    assert [row["id"] for row in cache.jobs(first)] == [job_first]
    assert [row["id"] for row in cache.jobs(second)] == [job_second]
    assert job(cache, first, job_first)["project"] == first
    assert job(cache, second, job_second)["project"] == second


def test_old_database_without_new_columns_is_migrated(tmp_path):
    directory, project = tmp_path / "cache", str(tmp_path)
    directory.mkdir()
    with sqlite3.connect(directory / "team.sqlite3") as db:
        db.executescript("""
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY, project TEXT, created REAL, path TEXT,
                original BLOB, changed BLOB, state TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, project TEXT, title TEXT, role TEXT,
                prompt TEXT, due INTEGER, required_file TEXT, state TEXT);
            CREATE TABLE journals (project TEXT, run TEXT, agent INTEGER, title TEXT, created REAL,
                content BLOB, PRIMARY KEY(project,run,agent));
        """)
        db.execute("INSERT INTO jobs(project,title,role,prompt,due,required_file,state) "
                   "VALUES(?,?,?,?,?,?,?)", (project, "Старое", "code", "Сделать", 4, "", "running"))

    cache = TeamCache(directory, clock=Clock())
    with cache.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
    assert {"owner", "started_at", "heartbeat", "reason"} <= columns

    row = cache.jobs(project)[0]
    assert (row["title"], row["due"], row["state"]) == ("Старое", 4, "running")
    assert row["owner"] is None and row["reason"] is None

    assert cache.recover() == 1  # owner=NULL трактуется как мёртвый владелец
    row = cache.jobs(project)[0]
    assert row["state"] == "paused"
    assert row["reason"] == REASON_INTERRUPTED
    assert (row["title"], row["prompt"]) == ("Старое", "Сделать")

    TeamCache(directory, clock=cache.clock)  # повторная инициализация безопасна
    assert cache.jobs(project)[0]["state"] == "paused"


def test_heartbeat_tracks_instance_liveness(tmp_path):
    clock = Clock()
    cache = TeamCache(tmp_path / "cache", clock=clock)
    beat = cache.heartbeat()
    assert beat == clock()
    with cache.connect() as db:
        stored = db.execute("SELECT heartbeat FROM instances WHERE token=?", (cache.owner,)).fetchone()
    assert stored["heartbeat"] == beat
    assert cache.alive(cache.owner) is True
    assert cache.alive("unknown-instance") is False
    assert cache.alive(None) is False

    clock.advance(STALE_AFTER + 1)
    assert cache.alive(cache.owner) is True

    cache.heartbeat()
    assert cache.alive(cache.owner) is True


def test_real_process_exit_is_recovered_without_waiting_for_heartbeat(tmp_path):
    import subprocess
    import sys

    script = (
        "import sys; from services.team_cache import TeamCache; "
        "c=TeamCache(sys.argv[1]); c.save_job('project','task','test','test',0,''); "
        "assert c.claim('project',c.jobs('project')[0]['id'])"
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path / "cache")], check=True)
    fresh = TeamCache(tmp_path / "cache")
    assert fresh.recover() == 1
    assert fresh.jobs("project")[0]["state"] == "paused"


def test_live_process_is_protected_when_heartbeat_expires(tmp_path):
    first = TeamCache(tmp_path / "cache", owner="first", clock=Clock())
    other = TeamCache(tmp_path / "cache", owner="other", clock=first.clock)
    first.save_job("project", "task", "code", "task", 0, "")
    ident = first.jobs("project")[0]["id"]
    first.claim("project", ident)
    first.clock.advance(STALE_AFTER * 10)
    assert other.recover() == 0
    with pytest.raises(ValueError):
        other.save_job("project", "changed", "code", "task", 0, "", ident)
    with pytest.raises(ValueError):
        other.remove_job("project", ident)
    assert not other.state("project", ident, "queued")
    assert not other.state("project", ident, "done", "running")
    assert first.jobs("project")[0]["state"] == "running"


def test_delete_and_claim_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    cache = TeamCache(tmp_path / "cache")
    cache.save_job("project", "task", "code", "task", 0, "")
    ident = cache.jobs("project")[0]["id"]
    other = TeamCache(tmp_path / "cache", owner="other")
    ready = Barrier(2)

    def claim():
        ready.wait()
        return cache.claim("project", ident)

    def remove():
        ready.wait()
        try:
            return other.remove_job("project", ident)
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = pool.submit(claim)
        removed = pool.submit(remove)
        assert claimed.result() != removed.result()
