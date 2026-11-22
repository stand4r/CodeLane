"""Connection profiles, local database discovery and SQL workbench operations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import csv
from io import StringIO
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import shutil
from time import perf_counter

from core.project_state import write_json

# SQLAlchemy (and the DBAPI drivers it pulls in) are *native* dependencies whose
# first import walks a deep import chain.  Resolve them once, on the import
# thread, instead of lazily inside ``DatabaseSession`` — that lazy import ran on
# a Qt thread-pool worker and intermittently killed the interpreter with
# "Windows fatal exception: access violation" inside importlib/re/sysconfig
# (see core/database_workspace.py history).  The workbench still degrades
# gracefully when the dependency is missing.
try:  # pragma: no cover - trivial import guard
    from sqlalchemy import (
        BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
        and_, create_engine, event, inspect, literal_column, select,
    )
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool
    from sqlalchemy.schema import CreateTable
    SQLALCHEMY_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - dependency missing
    (BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
     and_, create_engine, event, inspect, literal_column, select, URL, NullPool,
     CreateTable) = (None,) * 19
    SQLALCHEMY_ERROR = f"SQLAlchemy не установлен: {exc}"


ENGINES = {
    "sqlite": ("SQLite", "sqlite+pysqlite", 0),
    "postgresql": ("PostgreSQL", "postgresql+psycopg", 5432),
    "mysql": ("MySQL / MariaDB", "mysql+pymysql", 3306),
    "mssql": ("SQL Server", "mssql+pyodbc", 1433),
}
FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db3"}
CONNECTION_SUFFIX = ".dbconn"


@dataclass(frozen=True)
class DatabaseProfile:
    id: str
    name: str
    kind: str
    database: str
    host: str = ""
    port: int = 0
    user: str = ""
    password_env: str = ""
    trust_certificate: bool = False
    read_only: bool = False

    @classmethod
    def sqlite(cls, path):
        path = Path(path).resolve()
        return cls("sqlite:" + os.path.normcase(str(path)), path.name, "sqlite", str(path))

    @classmethod
    def load(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Неверный профиль Database")
        fields = cls.__dataclass_fields__
        profile = cls(**{key: data[key] for key in fields if key in data})
        for value in (profile.id, profile.name, profile.kind, profile.database, profile.host,
                      profile.user, profile.password_env):
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Неверный профиль Database")
        if (profile.kind not in ENGINES or not profile.database or not profile.id
                or type(profile.port) is not int or not 0 <= profile.port <= 65535
                or type(profile.trust_certificate) is not bool
                or type(profile.read_only) is not bool
                or (profile.password_env and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", profile.password_env))):
            raise ValueError("Неверный профиль Database")
        return profile

    def password(self, session_password=None):
        if session_password is not None:
            return session_password
        return os.environ.get(self.password_env) if self.password_env else None

    def url(self, password=None):
        if self.kind == "sqlite":
            path = Path(self.database).resolve()
            mode = "ro" if self.read_only else "rw"
            return URL.create(ENGINES[self.kind][1], database=path.as_uri(), query={"uri": "true", "mode": mode})
        query = {}
        if self.kind == "mssql":
            import pyodbc

            drivers = pyodbc.drivers()
            driver = next((name for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
                           if name in drivers), None)
            if driver is None:
                raise ValueError("Установите Microsoft ODBC Driver 17 или 18 for SQL Server")
            query = {"driver": driver, "Encrypt": "yes",
                     "TrustServerCertificate": "yes" if self.trust_certificate else "no"}
        return URL.create(ENGINES[self.kind][1], username=self.user or None, password=password,
                          host=self.host or "localhost", port=self.port or None,
                          database=self.database, query=query)


def connection_file_id(path):
    return "connection:" + os.path.normcase(str(Path(path).resolve()))


def read_connection_file(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("Файл подключения слишком большой")
    data = json.loads(raw.decode("utf-8-sig"))
    allowed = {"version", "name", "kind", "database", "host", "port", "user", "password_env", "trust_certificate"}
    if not isinstance(data, dict) or data.keys() - allowed or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Некорректный файл .dbconn")
    profile = DatabaseProfile.load({**data, "name": data.get("name") or Path(path).stem,
                                    "id": connection_file_id(path)})
    if profile.kind == "sqlite" or not profile.host:
        raise ValueError("Укажите серверную СУБД и хост")
    return profile


def write_connection_file(path, profile):
    data = asdict(profile)
    data.pop("id")
    data.pop("read_only")
    write_json(Path(path), {"version": 1, **data})


def project_connection_password(profile, project_root):
    if not project_root or not profile.password_env:
        return profile.password()
    host = profile.host.split("\\", 1)[0]
    try:
        local = ip_address(host.strip("[]")).is_loopback
    except ValueError:
        local = host.casefold() == "localhost"
    if local:
        root = Path(project_root).resolve()
        dotenv = (root / ".env").resolve()
        if dotenv.is_relative_to(root) and dotenv.is_file() and dotenv.stat().st_size <= 256 * 1024:
            from dotenv import dotenv_values

            values = dotenv_values(dotenv, interpolate=False, encoding="utf-8-sig")
            password = values.get(profile.password_env)
            if password is not None:
                return password
    return profile.password()


def read_profiles(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    profiles = {}
    for item in payload if isinstance(payload, list) else []:
        try:
            profile = DatabaseProfile.load(item)
        except (TypeError, ValueError):
            continue
        profiles[profile.id] = profile
    return profiles


def save_profiles(path, profiles):
    write_json(Path(path), [asdict(profile) for profile in profiles.values()])


def read_binding(root):
    if not root:
        return None, False
    try:
        payload = json.loads((Path(root) / ".codelane" / "database.json").read_text(encoding="utf-8"))
        profile = DatabaseProfile.load(payload["profile"])
        if profile.kind == "sqlite" and not Path(profile.database).is_absolute():
            path = (Path(root) / profile.database).resolve()
            profile = replace(profile, database=str(path), id=DatabaseProfile.sqlite(path).id)
        elif profile.kind != "sqlite" and isinstance(payload.get("connection_file"), str):
            path = (Path(root) / payload["connection_file"]).resolve()
            if path.is_relative_to(Path(root).resolve()):
                profile = replace(profile, id=connection_file_id(path))
        # A project file can select a profile for SQL, but cannot grant access to
        # process credentials. That preference is kept in local user settings.
        return profile, True
    except (OSError, TypeError, ValueError, KeyError):
        return None, False


def save_binding(root, profile):
    configuration = Path(root) / ".codelane" / "database.json"
    if profile is None:
        configuration.unlink(missing_ok=True)
        return
    if profile.kind == "sqlite":
        try:
            portable = Path(profile.database).resolve().relative_to(Path(root).resolve()).as_posix()
            profile = replace(profile, database=portable)
        except ValueError:
            pass
    payload = {"profile": asdict(profile)}
    if profile.id.startswith("connection:"):
        path = Path(profile.id.removeprefix("connection:"))
        if path.is_relative_to(Path(root).resolve()):
            payload["connection_file"] = path.relative_to(Path(root).resolve()).as_posix()
    write_json(configuration, payload)


def discover_local(root=None):
    """Read service registrations and project files without connecting to a server."""
    profiles = {}
    families = (
        ("postgresql", ("postgresql",), ("postgres",), "postgres", "postgres"),
        ("mysql", ("mysql", "mariadb"), ("mysqld", "mariadbd"), "mysql", "root"),
        ("mssql", ("mssqlserver", "mssql$"), ("sqlservr",), "master", ""),
    )
    services = {kind: {} for kind, *_ in families}
    install_roots = set()
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services") as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, index)
                    try:
                        with winreg.OpenKey(key, name) as service:
                            image = winreg.QueryValueEx(service, "ImagePath")[0]
                    except OSError:
                        continue
                    if not isinstance(image, str):
                        continue
                    for kind, prefixes, commands, _, _ in families:
                        executables = (*commands, "pg_ctl") if kind == "postgresql" else commands
                        if name.lower().startswith(prefixes) or any(
                            re.search(rf"(?i)\b{executable}\.exe\b", image) for executable in executables
                        ):
                            services[kind][name] = image
                            break
        except OSError:
            pass
        install_roots = {Path(os.environ[name]) for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                         if os.environ.get(name)}
    install_patterns = {
        "postgresql": ("PostgreSQL/*/bin/postgres.exe",),
        "mysql": ("MySQL/MySQL Server */bin/mysqld.exe", "MariaDB */bin/mariadbd.exe", "MariaDB */bin/mysqld.exe"),
        "mssql": ("Microsoft SQL Server/MSSQL*/MSSQL/Binn/sqlservr.exe",),
    }
    for kind, prefixes, commands, database, user in families:
        found = services[kind]
        if not found and (any(shutil.which(command) for command in commands)
                          or any(next(folder.glob(pattern), None)
                                 for folder in install_roots for pattern in install_patterns[kind])):
            found = {ENGINES[kind][0]: ""}
        for service, image in found.items():
            instance = service.split("$", 1)[1] if kind == "mssql" and "$" in service else ""
            host = "localhost" + ("\\" + instance if instance else "")
            port = 0 if instance else _service_port(kind, os.path.expandvars(image))
            profile = DatabaseProfile("local:" + service, service, kind, database, host,
                                      port, user)
            profiles[profile.id] = profile
    if root:
        profiles.update(discover_project_databases(root))
    return profiles


def discover_project_databases(root):
    from core.ignore_rules import walk_files

    profiles = {}
    for file in walk_files(Path(root), extensions=FILE_SUFFIXES, max_files=80, max_bytes=None).files:
        try:
            with file.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    continue
        except OSError:
            continue
        profile = DatabaseProfile.sqlite(file)
        profiles[profile.id] = profile
    return profiles


def _service_port(kind, command):
    if kind in {"postgresql", "mysql"}:
        inline = re.search(r"(?:\s--port(?:=|\s+)|\s-p\s+)(\d+)", command)
        if inline and 0 < int(inline[1]) <= 65535:
            return int(inline[1])
        pattern = r'\s-D\s+(?:"([^"]+)"|(\S+))' if kind == "postgresql" else (
            r'\s--defaults-file(?:=|\s+)(?:"([^"]+)"|(\S+))'
        )
        config = re.search(pattern, command, re.IGNORECASE)
        if config:
            path = Path(config[1] or config[2])
            if kind == "postgresql":
                path /= "postgresql.conf"
            if path.is_absolute() and not path.drive.startswith("\\\\"):
                try:
                    with path.open("rb") as stream:
                        source = stream.read(256 * 1024).decode("utf-8", errors="replace")
                    match = re.search(r"(?im)^\s*port\s*=\s*(\d+)", source)
                    if match and 0 < int(match[1]) <= 65535:
                        return int(match[1])
                except OSError:
                    pass
    return ENGINES[kind][2]


@dataclass(frozen=True)
class TableRef:
    schema: str
    name: str
    view: bool = False


@dataclass
class StatementResult:
    number: int
    columns: tuple
    rows: list
    truncated: bool


@dataclass
class QueryResult:
    columns: tuple
    rows: list
    elapsed_ms: int
    affected: int
    truncated: bool = False
    details: tuple = ()
    objects_changed: bool = False
    result_sets: tuple[StatementResult, ...] = ()


def csv_preview(columns, rows):
    def cell(value):
        if isinstance(value, bytes):
            return "0x" + value.hex()
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([cell(name) for name in columns])
    writer.writerows([cell(value) for value in row] for row in rows)
    return buffer.getvalue().encode("utf-8-sig")


class DatabaseSession:
    """Each operation borrows a connection and returns plain data to the UI."""

    def __init__(self, profile, session_password=None):
        if SQLALCHEMY_ERROR:
            raise RuntimeError(SQLALCHEMY_ERROR)

        self.profile = profile
        password = profile.password(session_password)
        url = profile.url(password)
        options = {
            "sqlite": {"timeout": 5},
            "postgresql": {"connect_timeout": 5},
            "mysql": {"connect_timeout": 5, "read_timeout": 60, "write_timeout": 60},
            "mssql": {"timeout": 5},
        }[profile.kind]
        self.engine = create_engine(url, connect_args=options, poolclass=NullPool, hide_parameters=True)
        if profile.kind == "sqlite":
            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys = ON")

    def schemas(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            default = inspector.default_schema_name
            schemas = inspector.get_schema_names()
            return list(dict.fromkeys(([default] if default else []) + schemas)) or [""]

    def databases(self):
        sql = {
            "postgresql": "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
                          "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname",
            "mysql": "SHOW DATABASES",
            "mssql": "SELECT name FROM sys.databases WHERE state = 0 AND HAS_DBACCESS(name) = 1 ORDER BY name",
        }.get(self.profile.kind)
        if not sql:
            return [self.profile.database]
        with self.engine.connect() as connection:
            return list(connection.exec_driver_sql(sql).scalars())

    def tables(self, schema):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            return ([TableRef(schema, name) for name in inspector.get_table_names(schema or None)]
                    + [TableRef(schema, name, True) for name in inspector.get_view_names(schema or None)])

    def _table(self, connection, ref):
        return Table(ref.name, MetaData(), schema=ref.schema or None, autoload_with=connection,
                     resolve_fks=False)

    def browse(self, ref, offset=0, limit=200):
        with self.engine.connect() as connection:
            table = self._table(connection, ref)
            primary = tuple(col.name for col in table.primary_key)
            query = select(table)
            if primary:
                query = query.order_by(*table.primary_key)
            elif self.profile.kind == "mssql":
                query = query.order_by(literal_column("(SELECT NULL)"))
            result = connection.execute(query.offset(offset).limit(limit + 1))
            rows = [tuple(row) for row in result.fetchmany(limit + 1)]
            return tuple(result.keys()), rows[:limit], primary, len(rows) > limit

    def structure(self, ref):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            schema = ref.schema or None
            columns = inspector.get_columns(ref.name, schema=schema)
            rows = [("Column", c["name"], str(c["type"]), "NULL" if c.get("nullable") else "NOT NULL",
                     str(c.get("default") or "")) for c in columns]
            if ref.view:
                rows.append(("View", ref.name, "", "", inspector.get_view_definition(ref.name, schema=schema) or ""))
                return rows
            key = inspector.get_pk_constraint(ref.name, schema=schema)
            if key.get("constrained_columns"):
                rows.append(("Primary key", key.get("name") or "", ", ".join(key["constrained_columns"]), "", ""))
            for item in inspector.get_indexes(ref.name, schema=schema):
                rows.append(("Index", item["name"], ", ".join(filter(None, item["column_names"])),
                             "UNIQUE" if item.get("unique") else "", ""))
            for item in inspector.get_foreign_keys(ref.name, schema=schema):
                target = ".".join(filter(None, [item.get("referred_schema"), item["referred_table"]]))
                rows.append(("Foreign key", item["name"] or "", ", ".join(item["constrained_columns"]),
                             "", target + " (" + ", ".join(item["referred_columns"]) + ")"))
            return rows

    def mutate(self, ref, action, values, original=None):
        if ref.view:
            raise ValueError("Редактирование представлений выполняется через SQL")
        with self.engine.begin() as connection:
            table = self._table(connection, ref)
            converted = {name: self._convert(table.c[name], value) for name, value in values.items()}
            if action == "insert":
                connection.execute(table.insert().values(converted))
                return
            if action not in {"update", "delete"}:
                raise ValueError("Неизвестная операция")
            if not table.primary_key or original is None:
                raise ValueError("Для редактирования и удаления требуется primary key")
            keys = set(col.name for col in table.primary_key)
            keys.update(values)
            where = and_(*(table.c[name] == original[name] for name in keys))
            statement = table.delete() if action == "delete" else table.update().values(converted)
            result = connection.execute(statement.where(where))
            if result.rowcount != 1:
                raise ValueError("Строка уже изменилась или была удалена. Обновите таблицу.")

    @staticmethod
    def _convert(column, value):
        import datetime
        from decimal import Decimal
        from uuid import UUID

        if value is None:
            return None
        try:
            kind = column.type.python_type
        except NotImplementedError:
            return value
        if isinstance(value, kind):
            return value
        if kind is bool:
            values = {"true": True, "false": False, "1": True, "0": False}
            if str(value).lower() in values:
                return values[str(value).lower()]
            raise ValueError(f"{column.name}: введите true или false")
        if kind in (datetime.datetime, datetime.date, datetime.time):
            return kind.fromisoformat(str(value))
        if kind is bytes:
            return bytes.fromhex(str(value).removeprefix("0x"))
        if kind in (str, int, float, Decimal, UUID):
            return kind(str(value))
        if isinstance(value, str) and kind in (dict, list):
            return json.loads(value)
        return value

    def run(self, sql, *, plan=False, autocommit=False, limit=1000):
        import sqlparse

        statements = sqlparse.split(sql)
        if not statements or (plan and len(statements) != 1):
            raise ValueError("Для EXPLAIN выберите один запрос" if plan else "Введите SQL")
        if plan:
            command = sqlparse.parse(statements[0])[0].get_type()
            if command not in {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"}:
                raise ValueError("EXPLAIN поддерживает SELECT, INSERT, UPDATE, DELETE и MERGE")
        result = QueryResult((), [], 0, -1)
        details = []
        result_sets = []
        remaining_preview = 4 * limit
        started = perf_counter()
        with self.engine.connect() as connection:
            if autocommit and not plan:
                connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            kind = self.profile.kind
            try:
                starts_transaction = re.match(r"(?is)^\s*BEGIN\b", sqlparse.format(statements[0], strip_comments=True))
                if kind == "sqlite" and not plan and not autocommit and not starts_transaction:
                    # Python's SQLite driver doesn't open a transaction for DDL.
                    connection.exec_driver_sql("BEGIN")
                if plan and kind == "mssql":
                    connection.exec_driver_sql("SET SHOWPLAN_XML ON")
                for number, statement in enumerate(statements, 1):
                    if not plan and re.match(r"(?is)^\s*(?:CREATE|ALTER|DROP|RENAME)\b",
                                             sqlparse.format(statement, strip_comments=True)):
                        result.objects_changed = True
                    if plan and kind != "mssql":
                        statement = ("EXPLAIN QUERY PLAN " if kind == "sqlite" else "EXPLAIN ") + statement
                    then = perf_counter()
                    cursor = connection.exec_driver_sql(statement)
                    result.columns = tuple(cursor.keys()) if cursor.returns_rows else ()
                    # Reserve a full preview for the last statement even in a long batch.
                    preview = limit if number == len(statements) else min(limit, remaining_preview)
                    rows = [tuple(row) for row in cursor.fetchmany(preview + 1)] if cursor.returns_rows else []
                    result.rows = rows[:preview]
                    result.truncated = len(rows) > preview
                    if result.columns:
                        result_sets.append(StatementResult(number, result.columns, result.rows, result.truncated))
                        remaining_preview -= len(result.rows)
                    result.affected = cursor.rowcount
                    cursor.close()
                    count = len(result.rows) if result.columns else result.affected
                    details.append((number, round((perf_counter() - then) * 1000),
                                    f"{count}+" if result.truncated else count))
                if plan and kind == "mssql":
                    connection.exec_driver_sql("SET SHOWPLAN_XML OFF")
                if not plan:
                    connection.commit()
            finally:
                if plan:
                    connection.rollback()
        result.elapsed_ms = round((perf_counter() - started) * 1000)
        result.details = tuple(details)
        result.result_sets = tuple(result_sets)
        return result

    def create_table_sql(self, schema, name, columns):
        types = {"INTEGER": Integer, "BIGINT": BigInteger, "VARCHAR": lambda: String(255),
                 "TEXT": Text, "FLOAT": Float, "BOOLEAN": Boolean, "TIMESTAMP": DateTime}
        if not name or "\x00" in name or not columns or len({item[0] for item in columns}) != len(columns):
            raise ValueError("Введите имя таблицы и уникальные имена колонок")
        if any(not column or "\x00" in column for column, *_ in columns):
            raise ValueError("Заполните имена колонок")
        table = Table(
            name, MetaData(),
            *(Column(column, types[kind](), primary_key=primary, nullable=not (required or primary))
              for column, kind, primary, required in columns),
            schema=schema or None,
        )
        return str(CreateTable(table).compile(dialect=self.engine.dialect)).strip() + ";"

    def select_table_sql(self, ref, limit=200):
        table = Table(ref.name, MetaData(), schema=ref.schema or None)
        query = select(literal_column("*")).select_from(table).limit(limit)
        return str(query.compile(dialect=self.engine.dialect, compile_kwargs={"literal_binds": True})).strip() + ";"
