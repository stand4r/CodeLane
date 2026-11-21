"""Start or stop the project's loopback-only PostgreSQL instance."""
from hashlib import sha256
from importlib.metadata import version
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import shutil
import sys

from dotenv import dotenv_values
import embedded_postgres
import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
LOCATION = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
STATE = LOCATION / "PyScribe" / "databases" / sha256(str(ROOT).encode()).hexdigest()[:16]
DATA = STATE / "postgres"
PACKAGE = Path(embedded_postgres.__file__).parent / "pginstall"
RUNTIME = STATE / f"runtime-{version('embedded-postgres')}" if os.name == "nt" else PACKAGE
BIN = RUNTIME / "bin"
STARTUP = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def pg(command, *arguments, check=True):
    suffix = ".exe" if os.name == "nt" else ""
    if command == "pg_ctl" and arguments[-1] == "start":
        # On Windows the daemon can inherit a pipe, keeping communicate()
        # blocked after pg_ctl exits. PostgreSQL writes to its own log instead.
        result = subprocess.run([str(BIN / (command + suffix)), *map(str, arguments)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=75, **STARTUP)
        if result.returncode:
            raise SystemExit(f"Не удалось запустить PostgreSQL. Журнал: {STATE / 'postgres.log'}")
        return result
    result = subprocess.run([str(BIN / (command + suffix)), *map(str, arguments)],
                            capture_output=True, **STARTUP)
    if check and result.returncode:
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        raise SystemExit(output.strip() or f"{command}: exit {result.returncode}")
    return result


def prepare_runtime():
    if RUNTIME != PACKAGE and not (RUNTIME / ".ready").is_file():
        # Windows PostgreSQL's bootstrap scripts may interpret the IDE's
        # installation path in the system codepage instead of UTF-8.
        shutil.copytree(PACKAGE, RUNTIME, dirs_exist_ok=True)
        (RUNTIME / ".ready").touch()


def start():
    STATE.mkdir(parents=True, exist_ok=True)
    prepare_runtime()
    environment = ROOT / ".env"
    if not environment.exists():
        with environment.open("x", encoding="utf-8") as stream:
            stream.write(f"DB_PASSWORD={token_urlsafe(32)}\n")
    password = dotenv_values(environment, interpolate=False).get("DB_PASSWORD")
    if not password:
        raise SystemExit("Укажите DB_PASSWORD в .env.")

    if not (DATA / "PG_VERSION").is_file():
        password_file = STATE / "init-password"
        with password_file.open("x", encoding="utf-8") as stream:
            stream.write(password + "\n")
        try:
            pg("initdb", "-D", DATA, "-U", "postgres", "--encoding=UTF8", "--locale=C",
               "--auth=scram-sha-256", f"--pwfile={password_file}")
        finally:
            password_file.unlink(missing_ok=True)

    if pg("pg_ctl", "-D", DATA, "status", check=False).returncode:
        pg("pg_ctl", "-D", DATA, "-l", STATE / "postgres.log", "-w",
           "-o", "-h 127.0.0.1 -p 5432", "start")

    connection = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
                  "password": password, "connect_timeout": 5}
    name = "diplom"
    with psycopg.connect(dbname="postgres", autocommit=True, **connection) as admin:
        installed_data = Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve()
        if installed_data != DATA.resolve():
            raise SystemExit("На порту 5432 работает другой экземпляр PostgreSQL.")
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(dbname=name, **connection) as db:
        tables = db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
        if not tables:
            db.execute((ROOT / "diplom.sql").read_text(encoding="utf-8-sig"))
    print("PostgreSQL: 127.0.0.1:5432/diplom")


if __name__ == "__main__":
    command = sys.argv[1:] or ["start"]
    if command == ["start"]:
        start()
    elif command == ["stop"]:
        pg("pg_ctl", "-D", DATA, "-w", "stop")
    else:
        raise SystemExit("Использование: diplom_postgres.py [start|stop]")
