"""Project-scoped schedules and recoverable snapshots of team changes."""
from contextlib import closing, contextmanager
from pathlib import Path
import os
import socket
import sqlite3
import time
import uuid
import zlib
import psutil

from services.agent_edits import FileEdit


#: Сколько секунд без heartbeat считать владельца задания мёртвым.
STALE_AFTER = 90

#: Причина приостановки задания, чей экземпляр CodeLane закрылся аварийно.
REASON_INTERRUPTED = ("Прервано: CodeLane был закрыт аварийно, задание больше не выполняется. "
                      "Запустите его заново или удалите.")

_PROCESS_TOKEN = None


def process_token():
    """Токен экземпляра процесса: ``pid-uuid4hex``, вычисляется один раз на процесс."""
    global _PROCESS_TOKEN
    if _PROCESS_TOKEN is None:
        _PROCESS_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex}"
    return _PROCESS_TOKEN


class TeamCache:
    def __init__(self, directory, owner=None, clock=None):
        """
        Открыть (и при необходимости создать) общее хранилище команды.

        Args:
            directory: каталог с файлом ``team.sqlite3``.
            owner: токен экземпляра; по умолчанию — токен текущего процесса.
            clock: функция времени (по умолчанию ``time.time``), подменяется в тестах.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "team.sqlite3"
        self.clock = clock or time.time
        self.owner = owner or process_token()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, project TEXT, created REAL, path TEXT,
                    original BLOB, changed BLOB, state TEXT, run TEXT);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, project TEXT, title TEXT, role TEXT,
                    prompt TEXT, due INTEGER, required_file TEXT, state TEXT);
                CREATE TABLE IF NOT EXISTS journals (
                    project TEXT, run TEXT, agent INTEGER, title TEXT, created REAL, content BLOB,
                    PRIMARY KEY(project,run,agent));
            """)
            if "run" not in {row["name"] for row in db.execute("PRAGMA table_info(snapshots)")}:
                db.execute("ALTER TABLE snapshots ADD COLUMN run TEXT")
            # Аддитивная миграция jobs: владелец, старт, heartbeat и причина паузы.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, kind in (("owner", "TEXT"), ("started_at", "REAL"),
                               ("heartbeat", "REAL"), ("reason", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            db.execute("CREATE TABLE IF NOT EXISTS instances ("
                       "token TEXT PRIMARY KEY, pid INTEGER, host TEXT, started REAL, heartbeat REAL)")
            if "process_created" not in {row["name"] for row in db.execute("PRAGMA table_info(instances)")}:
                db.execute("ALTER TABLE instances ADD COLUMN process_created REAL")
            db.execute("DELETE FROM snapshots WHERE created < ?", (time.time() - 14 * 86400,))
            db.execute("DELETE FROM journals WHERE created < ?", (time.time() - 14 * 86400,))

    @staticmethod
    def _host():
        """Имя хоста для строки instances (без исключений при сбое DNS)."""
        try:
            return socket.gethostname()
        except OSError:  # pragma: no cover - экзотические окружения
            return ""

    def heartbeat(self):
        """
        Отметить текущий экземпляр живым и обновить heartbeat его running-заданий.

        Returns:
            float: записанная метка времени.
        """
        now = self.clock()
        with self.connect() as db:
            db.execute("INSERT INTO instances(token,pid,host,started,heartbeat,process_created) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(token) DO UPDATE SET pid=excluded.pid,host=excluded.host,"
                       "heartbeat=excluded.heartbeat,process_created=excluded.process_created",
                       (self.owner, os.getpid(), self._host(), now, now, psutil.Process().create_time()))
            db.execute("UPDATE jobs SET heartbeat=? WHERE state='running' AND owner=?", (now, self.owner))
        return now

    def alive(self, token, stale_after=None):
        """
        Проверить, жив ли экземпляр-владелец задания.

        Args:
            token: токен экземпляра (``jobs.owner``).
            stale_after: порог устаревания в секундах, по умолчанию :data:`STALE_AFTER`.

        Returns:
            bool: локальный процесс с исходным временем создания жив либо
                heartbeat удалённого/старого экземпляра ещё актуален.
        """
        with self.connect() as db:
            return self._alive(db, token, stale_after)

    def _alive(self, db, token, stale_after=None):
        row = db.execute("SELECT * FROM instances WHERE token=?", (token,)).fetchone()
        if row is None:
            return False
        if row["host"] == self._host() and row["process_created"] is not None:
            try:
                process = psutil.Process(row["pid"])
                return process.create_time() == row["process_created"] and process.is_running()
            except psutil.NoSuchProcess:
                return False
            except psutil.AccessDenied:
                return True
        limit = self.clock() - (STALE_AFTER if stale_after is None else stale_after)
        return row["heartbeat"] is not None and row["heartbeat"] >= limit

    def recover(self, stale_after=None, project=None):
        """
        Приостановить задания, чей экземпляр CodeLane уже не работает.

        Переводит ``running`` → ``paused`` с причиной :data:`REASON_INTERRUPTED` для
        всех заданий (опционально — одного проекта), у которых владелец не текущий
        экземпляр и не подаёт признаков жизни. Идемпотентно.

        Args:
            stale_after: порог устаревания heartbeat в секундах.
            project: ограничить восстановление одним проектом.

        Returns:
            int: сколько заданий переведено в ``paused``.
        """
        stale = STALE_AFTER if stale_after is None else stale_after
        self.heartbeat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,project,owner FROM jobs WHERE state='running' "
                              "AND owner IS NOT ?", (self.owner,)).fetchall()
            if project is not None:
                rows = [row for row in rows if row["project"] == project]
            recovered = 0
            for row in rows:
                if self._alive(db, row["owner"], stale):
                    continue  # чужой, но живой экземпляр — не трогаем
                recovered += db.execute(
                    "UPDATE jobs SET state='paused',reason=? WHERE id=? AND project=? "
                    "AND state='running' AND owner IS ?",
                    (REASON_INTERRUPTED, row["id"], row["project"], row["owner"])).rowcount
        return recovered

    def claim(self, project, job):
        """
        Атомарно забрать задание в работу: ``queued`` → ``running`` за текущим экземпляром.

        Returns:
            bool: True, если задание действительно захвачено этим вызовом.
        """
        return self._claim(project, job, "queued")

    def _claim(self, project, job, expected):
        """CAS-захват задания из состояния ``expected`` с записью владельца и heartbeat."""
        if expected not in {"queued", "paused"}:
            return False
        now = self.clock()
        self.heartbeat()
        with self.connect() as db:
            return db.execute(
                "UPDATE jobs SET state='running',owner=?,started_at=?,heartbeat=?,reason=NULL "
                "WHERE id=? AND project=? AND state=?",
                (self.owner, now, now, job, project, expected)).rowcount == 1

    def archive(self, project, run, journals):
        now = time.time()
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO journals VALUES(?,?,?,?,?,?)",
                           ((project, run, number, title, now, zlib.compress(text.encode()))
                            for number, title, text in journals))

    def journals(self, project):
        with self.connect() as db:
            return db.execute("SELECT run,agent,title,created FROM journals WHERE project=? AND created>=? "
                              "ORDER BY created DESC,agent", (project, time.time() - 14 * 86400)).fetchall()

    def journal(self, project, run, agent):
        with self.connect() as db:
            row = db.execute("SELECT content FROM journals WHERE project=? AND run=? AND agent=? AND created>=?",
                             (project, run, agent, time.time() - 14 * 86400)).fetchone()
        return zlib.decompress(row[0]).decode() if row else ""

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def snapshots(self, project):
        with self.connect() as db:
            db.execute("DELETE FROM snapshots WHERE created < ?", (time.time() - 14 * 86400,))
            return db.execute("SELECT id,path,created,state,run FROM snapshots WHERE project=? ORDER BY id DESC",
                              (project,)).fetchall()

    def apply(self, project, edit, toolbox, run=None):
        with self.connect() as db:
            snapshot = db.execute("INSERT INTO snapshots(project,created,path,original,changed,state,run) VALUES(?,?,?,?,?,?,?)",
                                  (project, time.time(), edit.path, zlib.compress(edit.before.encode()),
                                   zlib.compress(edit.after.encode()), "pending", run)).lastrowid
        try:
            message = edit.apply(toolbox)
        except Exception:
            with self.connect() as db:
                db.execute("DELETE FROM snapshots WHERE id=?", (snapshot,))
            raise
        with self.connect() as db:
            db.execute("UPDATE snapshots SET state='applied' WHERE id=?", (snapshot,))
        return message

    def restore_run(self, project, run, toolbox):
        rows = [r for r in self.snapshots(project) if r["run"] == run and r["state"] != "restored"]
        if not run or not rows:
            raise ValueError("В этом запуске нет изменений для отката.")
        files = {}
        for row in rows:
            edit = self.contents(project, row["id"])[0]
            latest = files.get(edit.path)
            if latest is not None and edit.after != latest.before:
                raise ValueError(f"История изменений файла содержит разрыв: {edit.path}.")
            files[edit.path] = FileEdit(edit.path, edit.before, latest.after if latest else edit.after)
        entries = [(path, edit) for path, edit in files.items()]
        for _, edit in entries:
            if toolbox.preview_edit_source(edit.path)[1] != edit.after:
                raise ValueError(f"Файл изменился после применения: {edit.path}. Откат не начат.")
        restored = []
        try:
            for _, edit in entries:
                FileEdit(edit.path, edit.after, edit.before).apply(toolbox)
                restored.append(edit)
            with self.connect() as db:
                db.executemany("UPDATE snapshots SET state='restored' WHERE id=?",
                               ((row["id"],) for row in rows))
        except Exception as exc:
            incomplete = []
            for edit in reversed(restored):
                try:
                    edit.apply(toolbox)
                except Exception:
                    incomplete.append(edit.path)
            if incomplete:
                raise RuntimeError(f"Откат прерван: {exc}. Проверьте файлы: {', '.join(incomplete)}") from exc
            raise
        return f"Восстановлено файлов: {len(restored)}."

    def contents(self, project, snapshot):
        with self.connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=? AND project=? AND created>=?",
                             (snapshot, project, time.time() - 14 * 86400)).fetchone()
        if row is None:
            raise ValueError("Снимок недоступен.")
        return FileEdit(row["path"], zlib.decompress(row["original"]).decode(),
                        zlib.decompress(row["changed"]).decode()), row["state"]

    def restore(self, project, snapshot, toolbox):
        edit, state = self.contents(project, snapshot)
        if state == "restored":
            raise ValueError("Снимок уже восстановлен.")
        message = FileEdit(edit.path, edit.after, edit.before).apply(toolbox)
        with self.connect() as db:
            db.execute("UPDATE snapshots SET state='restored' WHERE id=?", (snapshot,))
        return message

    def jobs(self, project):
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE project=? ORDER BY due,id", (project,)).fetchall()

    def save_job(self, project, title, role, prompt, due, required_file, job=None):
        if required_file:
            root = Path(project).resolve()
            if not (root / required_file).resolve().is_relative_to(root):
                raise ValueError("Файл условия должен находиться в проекте.")
        with self.connect() as db:
            if job is None:
                db.execute("INSERT INTO jobs(project,title,role,prompt,due,required_file,state) "
                           "VALUES(?,?,?,?,?,?,'queued')",
                           (project, title, role, prompt, due, required_file))
            else:
                # Сохранение задания — это и есть повторный запуск: чистим следы прошлого владельца.
                changed = db.execute("UPDATE jobs SET title=?,role=?,prompt=?,due=?,required_file=?,state='queued',"
                           "reason=NULL,owner=NULL,started_at=NULL,heartbeat=NULL "
                           "WHERE id=? AND project=? AND state IS NOT 'running'",
                           (title, role, prompt, due, required_file, job, project)).rowcount
                if not changed:
                    raise ValueError("Задание выполняется, удалено или принадлежит другому проекту.")

    def remove_job(self, project, job):
        """Удаляет задание проекта. False — записи нет, ValueError — задание выполняет живой экземпляр."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,owner FROM jobs WHERE id=? AND project=?", (job, project)).fetchone()
            if row is None:
                return False
            if row["state"] == "running" and self._alive(db, row["owner"]):
                raise ValueError("Задание выполняется и его нельзя удалить.")
            return db.execute("DELETE FROM jobs WHERE id=? AND project=?", (job, project)).rowcount == 1

    def state(self, project, job, state, expected=None, reason=None):
        """
        Перевести задание в новое состояние.

        ``running`` делегируется в :meth:`claim` (владелец, время старта и heartbeat),
        ``paused``/``done`` пишут причину (``done`` всегда очищает её), ``queued``
        сбрасывает владельца и heartbeat — это повторный запуск.

        Args:
            project: корень проекта.
            job: идентификатор задания.
            state: целевое состояние.
            expected: ожидаемое текущее состояние (CAS); None — без проверки.
            reason: причина для ``paused``.

        Returns:
            bool: True, если строка изменилась этим вызовом.
        """
        if state == "running":
            return self._claim(project, job, expected or "queued")
        if state == "queued":
            query = ("UPDATE jobs SET state=?,reason=NULL,owner=NULL,started_at=NULL,heartbeat=NULL "
                     "WHERE id=? AND project=? AND (? IS NULL OR state=?)")
            args = (state, job, project, expected, expected)
        elif state in ("paused", "done"):
            query = "UPDATE jobs SET state=?,reason=? WHERE id=? AND project=? AND (? IS NULL OR state=?)"
            args = (state, None if state == "done" else reason, job, project, expected, expected)
        else:
            query = "UPDATE jobs SET state=? WHERE id=? AND project=? AND (? IS NULL OR state=?)"
            args = (state, job, project, expected, expected)
        with self.connect() as db:
            query += " AND (state IS NOT 'running' OR owner=?)" if state != "queued" else " AND state IS NOT 'running'"
            if state != "queued":
                args = (*args, self.owner)
            return db.execute(query, args).rowcount == 1
