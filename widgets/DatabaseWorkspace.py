"""Database explorer, connection profiles and SQL editor."""
from dataclasses import replace
import os
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from core.database_workspace import (
    CONNECTION_SUFFIX, ENGINES, DatabaseProfile, DatabaseSession, connection_file_id, csv_preview, discover_local,
    project_connection_password, read_binding, read_connection_file, read_profiles,
    save_binding, save_profiles, write_connection_file,
)
from services.background import run_async


def button(layout, text, callback):
    widget = QtWidgets.QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


class ServerProfileDialog(QtWidgets.QDialog):
    def __init__(self, parent, profile=None, installations=()):
        super().__init__(parent)
        self.setWindowTitle("Файл подключения")
        self.setMinimumWidth(390)
        self.profile = None
        form = QtWidgets.QFormLayout(self)
        self.kind = QtWidgets.QComboBox()
        for kind in ("postgresql", "mysql", "mssql"):
            self.kind.addItem(ENGINES[kind][0], kind)
        self.name = QtWidgets.QLineEdit()
        self.host = QtWidgets.QLineEdit("localhost")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("Auto")
        self.port.setValue(5432)
        self.database = QtWidgets.QLineEdit()
        self.user = QtWidgets.QLineEdit()
        self.password_env = QtWidgets.QLineEdit()
        self.trust = QtWidgets.QCheckBox("Доверять сертификату SQL Server")
        self.trust.hide()
        self.error = QtWidgets.QLabel()
        if installations:
            found = QtWidgets.QComboBox()
            found.addItem("Вручную", None)
            for item in installations:
                found.addItem(item.name, item)
            found.currentIndexChanged.connect(lambda: self._load(found.currentData()))
            form.addRow("Локальная СУБД", found)
        for caption, widget in (
            ("СУБД", self.kind), ("Имя", self.name), ("Хост", self.host), ("Порт", self.port),
            ("База", self.database), ("Пользователь", self.user), ("Переменная пароля", self.password_env),
        ):
            form.addRow(caption, widget)
        form.addRow(self.trust)
        form.addRow(self.error)
        actions = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        form.addRow(actions)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._load(profile)

    def _kind_changed(self):
        kind = self.kind.currentData()
        self.port.setValue(ENGINES[kind][2])
        self.trust.setVisible(kind == "mssql")

    def _load(self, profile):
        if profile is None:
            return
        self.kind.setCurrentIndex(self.kind.findData(profile.kind))
        self.name.setText(profile.name)
        self.host.setText(profile.host)
        self.port.setValue(profile.port)
        self.database.setText(profile.database)
        self.user.setText(profile.user)
        self.password_env.setText(profile.password_env)
        self.trust.setChecked(profile.trust_certificate)

    def accept(self):
        normalize = lambda field: field.text().strip()
        try:
            self.profile = DatabaseProfile.load({
                "id": "draft", "kind": self.kind.currentData(), "host": normalize(self.host),
                "name": normalize(self.name), "port": self.port.value(), "database": normalize(self.database),
                "user": normalize(self.user), "password_env": normalize(self.password_env),
                "trust_certificate": self.kind.currentData() == "mssql" and self.trust.isChecked(),
            })
            if not self.profile.host:
                raise ValueError("Укажите хост")
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()


class ResultModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns = ()
        self.rows = []

    def set_data(self, columns, rows):
        self.beginResetModel()
        self.columns, self.rows = columns, rows
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
            return None
        value = self.rows[index.row()][index.column()]
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            value = "0x" + value[:2048].hex() + ("…" if len(value) > 2048 else "")
        else:
            value = str(value)
        return value if len(value) <= 4096 else value[:4096] + "…"

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self.columns[section] if orientation == QtCore.Qt.Orientation.Horizontal else str(section + 1)
        return None


class DatabaseManager(QtCore.QObject):
    local_installations_changed = QtCore.pyqtSignal()
    runtime_changed = QtCore.pyqtSignal()

    def __init__(self, window):
        super().__init__(window)
        self.main_window = window
        data_root = (Path(os.environ.get("APPDATA", Path.home())) / "PyScribe"
                     if os.name == "nt" else Path.home() / ".config" / "PyScribe")
        self.inventory_path = data_root / "database-installations.json"
        self.saved = read_profiles(data_root / "database-connections.json")
        self.local_installations = read_profiles(self.inventory_path)
        self.local_scan_running = False
        self.local_scan_error = ""
        self.root = None
        self.bound = None
        self.runtime_profile = None
        self.runtime_password = None

    def set_project(self, root):
        root = Path(root).resolve() if root else None
        if root != self.root:
            self.root = root
            self.bound, _ = read_binding(root)
            self.runtime_profile = None
            self.runtime_password = None
            self.runtime_changed.emit()

    def discover_local_installations(self):
        if self.local_scan_running:
            return
        self.local_scan_running = True
        self.local_scan_error = ""
        self.local_installations_changed.emit()
        target = self.inventory_path

        def scan():
            profiles = discover_local()
            try:
                save_profiles(target, profiles)
            except OSError as exc:
                return profiles, str(exc)
            return profiles, ""

        def finished(result):
            self.local_installations, self.local_scan_error = result
            self.local_scan_running = False
            self.local_installations_changed.emit()

        run_async(scan, on_done=finished,
                  on_error=lambda message: finished((self.local_installations, message)))

    def _open_tabs(self):
        for group in self.main_window.editor_groups():
            for index in range(group.count()):
                widget = group.widget(index)
                if isinstance(widget, DatabaseWorkbench):
                    yield group, index, widget

    def _focus(self, group, index):
        self.main_window._set_active_group(group)
        group.setCurrentIndex(index)

    def open_sqlite_path(self, path):
        profile = DatabaseProfile.sqlite(path)
        if self.bound and self.bound.id == profile.id:
            profile = self.bound
        else:
            profile = self.saved.get(profile.id, profile)
        return self._open_file_tab(path, profile)

    def open_connection_path(self, path):
        profile = read_connection_file(path)
        return self._open_file_tab(path, profile, server=True)

    def new_connection_file(self):
        dialog = ServerProfileDialog(self.main_window, installations=self.local_installations.values())
        if not dialog.exec():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.main_window, "Сохранить подключение", str(self.root or Path.cwd()), "Database (*.dbconn)"
        )
        if not path:
            return
        appended_suffix = Path(path).suffix.lower() != CONNECTION_SUFFIX
        if appended_suffix:
            path += CONNECTION_SUFFIX
        # The file dialog cannot confirm overwriting a suffix appended afterwards.
        if appended_suffix and Path(path).exists():
            answer = QtWidgets.QMessageBox.question(self.main_window, "Database", "Перезаписать файл подключения?")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        try:
            write_connection_file(path, dialog.profile)
            self.open_connection_path(path)
        except (OSError, ValueError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self.main_window, "Database", str(exc))

    def _open_file_tab(self, path, profile, *, server=False):
        for group, index, widget in self._open_tabs():
            if widget.database_id == profile.id:
                self._focus(group, index)
                if not widget.working:
                    if server and widget.server_profile != profile:
                        widget._reset_connection()
                        widget.show_server_profile(profile)
                    elif not server and widget.session is None:
                        widget.connect_profile(profile, None)
                return widget
        widget = DatabaseWorkbench(self, profile.id, Path(path) if server else None)
        group = self.main_window.active_tab_widget
        index = group.addTab(widget, Path(path).name)
        group.setTabToolTip(index, str(path))
        self._focus(group, index)
        if server:
            widget.show_server_profile(profile)
        else:
            widget.connect_profile(profile, None)
        return widget

    def bind(self, profile):
        if not self.root:
            raise ValueError("Откройте проект")
        save_binding(self.root, profile)
        self.bound = profile
        self.runtime_profile = None
        self.runtime_password = None
        self.runtime_changed.emit()

    def set_runtime(self, profile, enabled, password=None):
        if enabled and (not self.root or profile != self.bound):
            raise ValueError("Сначала привяжите это подключение к проекту")
        if enabled or self.runtime_profile == profile:
            self.runtime_profile = profile if enabled else None
            self.runtime_password = password if enabled else None
            self.runtime_changed.emit()

    def runtime_url(self, cwd):
        if not self.root or not self.runtime_profile:
            return None
        try:
            if not Path(cwd).resolve().is_relative_to(self.root):
                return None
            profile = self.runtime_profile
            return profile.url(profile.password(self.runtime_password)).render_as_string(hide_password=False)
        except (OSError, ValueError, ImportError):
            return None

    def _project_database_path(self, source_path):
        source = Path(source_path).resolve() if source_path else None
        if source and self.root and not source.is_relative_to(self.root):
            return None
        if self.bound:
            if self.bound.kind == "sqlite":
                return Path(self.bound.database)
            if self.bound.id.startswith("connection:"):
                return Path(self.bound.id.removeprefix("connection:"))
            return None
        root = self.root or (source.parent if source else None)
        if root is None or (source and not source.is_relative_to(root)):
            return None
        candidates = [root / ".codelane" / "database.dbconn", root / "database.dbconn"]
        if source:
            candidates.insert(0, source.with_suffix(CONNECTION_SUFFIX))
        for candidate in candidates:
            path = candidate.resolve()
            if path.is_relative_to(root) and path.is_file():
                return path
        return None

    def open_sql(self, source, source_path=None):
        if source_path and self.root and not Path(source_path).resolve().is_relative_to(self.root):
            return False
        path = self._project_database_path(source_path)
        if path is not None:
            if not path.is_file():
                return False
            try:
                if path.suffix.lower() == CONNECTION_SUFFIX:
                    widget = self.open_connection_path(path)
                else:
                    widget = self.open_sqlite_path(path)
            except (OSError, TypeError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self.main_window, "Database", str(exc))
                return True
            if widget.working and widget.session is not None:
                widget.status.setText("Операция уже выполняется…")
            else:
                widget.load_sql(source)
            return True
        tabs = [tab for tab in self._open_tabs() if tab[2].session is not None]
        if self.bound:
            target = next((tab for tab in tabs if tab[2].profile == self.bound), None)
        else:
            target = tabs[0] if len(tabs) == 1 else None
        if target is None:
            return False
        group, index, widget = target
        self._focus(group, index)
        if widget.working:
            widget.status.setText("Операция уже выполняется…")
        else:
            widget.load_sql(source)
        return True


class DatabaseWorkbench(QtWidgets.QWidget):
    PAGE_SIZE = 200

    def __init__(self, panel, database_id, connection_path=None):
        super().__init__(panel.main_window)
        self.panel = panel
        self.database_id = database_id
        self.connection_path = connection_path
        self.server_profile = None
        self.session_password = None
        self.session = None
        self.profile = None
        self.working = False
        self.table = None
        self.offset = 0
        self.primary = ()
        self.more_rows = False
        self.setProperty("pyscribeSpecial", "database")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        toolbar = QtWidgets.QHBoxLayout()
        self.connection = QtWidgets.QLabel("Database")
        self.connection.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        toolbar.addWidget(self.connection, 1)
        if connection_path:
            button(toolbar, "Подключить", self.connect_server)
            button(toolbar, "Отключить", self.disconnect_server)
            button(toolbar, "Изменить", self.edit_server_profile)
        button(toolbar, "Привязать к проекту", self.bind)
        self.runtime = QtWidgets.QCheckBox("DATABASE_URL при запуске")
        self.runtime.setToolTip("Передаёт параметры и пароль только запускаемому коду этого проекта")
        self.runtime.toggled.connect(self._set_runtime)
        self.panel.runtime_changed.connect(self._sync_runtime)
        toolbar.addWidget(self.runtime)
        layout.addLayout(toolbar)
        body = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        layout.addWidget(body, 1)
        explorer = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(explorer)
        column.setContentsMargins(0, 0, 6, 0)
        self.schemas = QtWidgets.QComboBox()
        self.schemas.currentIndexChanged.connect(self.refresh_tables)
        column.addWidget(self.schemas)
        self.object_filter = QtWidgets.QLineEdit()
        self.object_filter.setPlaceholderText("Таблицы и представления")
        self.object_filter.setClearButtonEnabled(True)
        self.object_filter.textChanged.connect(self._filter_objects)
        column.addWidget(self.object_filter)
        self.objects = QtWidgets.QListWidget()
        self.objects.itemSelectionChanged.connect(self._select_table)
        self.objects.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.objects.customContextMenuRequested.connect(self._object_menu)
        column.addWidget(self.objects, 1)
        object_actions = QtWidgets.QHBoxLayout()
        button(object_actions, "Обновить", self.refresh_catalog)
        button(object_actions, "+ Таблица", self.create_table)
        button(object_actions, "DDL…", self._templates)
        column.addLayout(object_actions)
        body.addWidget(explorer)

        self.pages = QtWidgets.QTabWidget()
        body.addWidget(self.pages)
        body.setStretchFactor(1, 1)
        body.setSizes([230, 760])

        page = QtWidgets.QWidget()
        sql_layout = QtWidgets.QVBoxLayout(page)
        tools = QtWidgets.QHBoxLayout()
        self.run_button = button(tools, "Выполнить", self.run_sql)
        self.run_button.setToolTip("Ctrl+Enter")
        button(tools, "EXPLAIN", lambda: self.run_sql(plan=True))
        button(tools, "Сохранить SQL…", self.save_sql)
        self.autocommit = QtWidgets.QCheckBox("Autocommit")
        self.autocommit.setToolTip("Для команд вне транзакции, например CREATE DATABASE и VACUUM")
        tools.addWidget(self.autocommit)
        tools.addStretch(1)
        sql_layout.addLayout(tools)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.sql = QtWidgets.QPlainTextEdit()
        self.sql.setPlaceholderText("SQL")
        from core.language_services import LanguageProviderFactory
        from widgets.QCodeEditor import ModernSyntaxHighlighter
        config = LanguageProviderFactory.get_language_config("sql")
        self.highlighter = ModernSyntaxHighlighter(self.sql.document(), config)
        split.addWidget(self.sql)
        self.output_tabs = QtWidgets.QTabWidget()
        self.results, self.result_model = self._grid()
        self.results.doubleClicked.connect(self._show_result_cell)
        self.execution, self.execution_model = self._grid()
        self.execution.doubleClicked.connect(self._jump_to_result)
        results_page = QtWidgets.QWidget()
        results_layout = QtWidgets.QVBoxLayout(results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.result_selector = QtWidgets.QComboBox()
        self.result_selector.currentIndexChanged.connect(self._show_selected_result)
        self.result_selector.hide()
        result_tools = QtWidgets.QHBoxLayout()
        result_tools.addWidget(self.result_selector)
        result_tools.addStretch(1)
        self.csv_button = button(result_tools, "CSV…", self.export_csv)
        self.csv_button.setEnabled(False)
        results_layout.addLayout(result_tools)
        results_layout.addWidget(self.results, 1)
        self.output_tabs.addTab(results_page, "Результат")
        self.output_tabs.addTab(self.execution, "Выполнение")
        split.addWidget(self.output_tabs)
        split.setSizes([260, 340])
        sql_layout.addWidget(split, 1)
        self.pages.addTab(page, "SQL")
        self.run_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.sql)
        self.run_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        self.run_shortcut.activated.connect(self.run_sql)

        page = QtWidgets.QWidget()
        data = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        self.prev = button(row, "‹", lambda: self._page(-1))
        self.next = button(row, "›", lambda: self._page(1))
        self.page_label = QtWidgets.QLabel()
        row.addWidget(self.page_label, 1)
        self.add_row = button(row, "+ Строка", lambda: self.change_row("insert"))
        self.edit_cell = button(row, "Изменить ячейку", lambda: self.change_row("update"))
        self.delete_row = button(row, "Удалить строку", lambda: self.change_row("delete"))
        data.addLayout(row)
        self.records, self.record_model = self._grid()
        self.records.doubleClicked.connect(lambda _: self.change_row("update"))
        self.records.selectionModel().currentChanged.connect(self._update_row_actions)
        data.addWidget(self.records, 1)
        self.pages.addTab(page, "Данные")
        grid, self.structure_model = self._grid()
        self.pages.addTab(grid, "Структура")
        self.pages.currentChanged.connect(self._load_page)
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._update_row_actions()

    def _grid(self):
        view = QtWidgets.QTableView()
        model = ResultModel(view)
        view.setModel(model)
        view.setAlternatingRowColors(True)
        view.setSortingEnabled(False)
        view.horizontalHeader().setStretchLastSection(True)
        return view, model

    def _job(self, action, complete):
        if self.working:
            self.status.setText("Операция уже выполняется…")
            return False
        self.working = True
        self.status.setText("Выполняется…")
        self._update_row_actions()

        def done(value):
            self.working = False
            self.status.clear()
            self._update_row_actions()
            complete(value)

        def failed(message):
            self.working = False
            self.status.setText(message)
            self._update_row_actions()

        run_async(action, on_done=done, on_error=failed)
        return True

    def connect_profile(self, profile, password):
        def connect():
            session = DatabaseSession(profile, password)
            return session, session.schemas()

        def ready(payload):
            self._clear_query_results()
            self.session, schemas = payload
            self.profile = profile
            self.session_password = password
            self.connection.setText(f"{profile.name} · {ENGINES[profile.kind][0]}")
            self.runtime.blockSignals(True)
            self.runtime.setChecked(self.panel.runtime_profile == profile)
            self.runtime.blockSignals(False)
            self._clear_objects()
            self._apply_schemas(schemas)

        self._job(connect, ready)

    def show_server_profile(self, profile):
        self.server_profile = profile
        self.connection.setText(f"{profile.name or profile.database} · {profile.host}")
        self.status.setText("Подключение закрыто")

    def connect_server(self):
        if self.working:
            return
        try:
            profile = read_connection_file(self.connection_path)
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        endpoint = f"{profile.host}:{profile.port}" if profile.port else profile.host
        caption = f"{ENGINES[profile.kind][0]} · {endpoint} / {profile.database}"
        if profile.user:
            caption += f" · {profile.user}"
        root = self.panel.root or self.connection_path.parent
        if not self.connection_path.resolve().is_relative_to(root):
            root = None
        if profile.password_env:
            caption += f"\nПароль можно указать через {profile.password_env}"
        password, accepted = QtWidgets.QInputDialog.getText(
            self, "Подключиться", caption, QtWidgets.QLineEdit.EchoMode.Password,
        )
        if accepted:
            try:
                secret = password or project_connection_password(profile, root)
            except (OSError, UnicodeError) as exc:
                self.status.setText(str(exc))
                return
            self._reset_connection()
            self.show_server_profile(profile)
            self.connect_profile(profile, secret)

    def edit_server_profile(self):
        if self.working:
            return
        try:
            current = read_connection_file(self.connection_path)
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        dialog = ServerProfileDialog(self, current)
        if not dialog.exec():
            return
        try:
            write_connection_file(self.connection_path, dialog.profile)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        profile = replace(dialog.profile, id=connection_file_id(self.connection_path))
        self._reset_connection()
        self.show_server_profile(profile)
        if self.panel.bound == current:
            try:
                self.panel.bind(profile)
            except (OSError, ValueError) as exc:
                self.status.setText(str(exc))

    def disconnect_server(self):
        if not self.working:
            self._reset_connection()
            self.show_server_profile(self.server_profile)

    def _reset_connection(self):
        if self.profile is not None:
            self.panel.set_runtime(self.profile, False)
        self.session = self.profile = self.session_password = None
        with QtCore.QSignalBlocker(self.schemas):
            self.schemas.clear()
        self._clear_objects()
        self._clear_query_results()
        self._sync_runtime()

    def _clear_objects(self):
        with QtCore.QSignalBlocker(self.objects):
            self.objects.clear()
        self.table = None
        self.primary = ()
        self.more_rows = False
        self.offset = 0
        self.record_model.set_data((), [])
        self.structure_model.set_data((), [])
        self.page_label.clear()
        self._update_row_actions()

    def refresh_catalog(self, checked=False, *, notice=""):
        if self.session:
            self._job(self.session.schemas, lambda schemas: self._apply_schemas(schemas, notice=notice))

    def _apply_schemas(self, schemas, *, notice=""):
        current = self.schemas.currentData()
        with QtCore.QSignalBlocker(self.schemas):
            self.schemas.clear()
            for name in schemas:
                self.schemas.addItem(name or "default", name)
            previous = self.schemas.findData(current)
            if previous >= 0:
                self.schemas.setCurrentIndex(previous)
        self.refresh_tables(notice=notice)

    def _filter_objects(self):
        query = self.object_filter.text().strip().casefold()
        for row in range(self.objects.count()):
            item = self.objects.item(row)
            item.setHidden(query not in item.text().casefold())

    def refresh_tables(self, _index=None, *, notice=""):
        session = self.session
        schema = self.schemas.currentData()
        if not session or schema is None:
            return
        previous = self.table

        def ready(objects):
            self._clear_objects()
            with QtCore.QSignalBlocker(self.objects):
                for ref in objects:
                    item = QtWidgets.QListWidgetItem(ref.name + (" · View" if ref.view else ""))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, ref)
                    self.objects.addItem(item)
                    if ref == previous:
                        self.objects.setCurrentItem(item)
            self._filter_objects()
            if self.objects.currentItem():
                self._select_table(show_data=False)
            if notice:
                self.status.setText(notice)

        self._job(lambda: session.tables(schema), ready)

    def _select_table(self, *, show_data=True):
        item = self.objects.currentItem()
        self.table = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        self.offset = 0
        self.primary = ()
        self.more_rows = False
        self.record_model.set_data((), [])
        self.structure_model.set_data((), [])
        self._update_row_actions()
        if show_data and self.pages.currentIndex() == 0:
            self.pages.setCurrentIndex(1)
        else:
            self._load_page()

    def _object_menu(self, position):
        item = self.objects.itemAt(position)
        if item is None or self.session is None or self.working:
            return
        ref = item.data(QtCore.Qt.ItemDataRole.UserRole)
        menu = QtWidgets.QMenu(self)
        draft = menu.addAction("Открыть SELECT в SQL")
        if menu.exec(self.objects.viewport().mapToGlobal(position)) == draft:
            if self.load_sql(self.session.select_table_sql(ref, self.PAGE_SIZE)):
                self.sql.document().setModified(True)

    def _load_page(self, _index=None):
        session, ref = self.session, self.table
        if not session or not ref:
            return
        if self.pages.currentIndex() == 1:
            offset = self.offset

            def ready(data):
                if self.table == ref and self.offset == offset:
                    columns, rows, self.primary, self.more_rows = data
                    self.record_model.set_data(columns, rows)
                    self.page_label.setText(f"{offset + 1}–{offset + len(rows)}" if rows else "0")
                    self._update_row_actions()
                else:
                    self._load_page()

            self._job(lambda: session.browse(ref, offset, self.PAGE_SIZE), ready)
        elif self.pages.currentIndex() == 2:
            def ready(rows):
                if self.table == ref:
                    self.structure_model.set_data(("Объект", "Имя", "Тип / колонки", "Свойства", "DDL"), rows)
                else:
                    self._load_page()

            self._job(lambda: session.structure(ref), ready)

    def _page(self, direction):
        if not self.working:
            self.offset = max(0, self.offset + self.PAGE_SIZE * direction)
            self._load_page()

    def _update_row_actions(self, *_args):
        table = self.table
        can_write = bool(table and not table.view and self.profile and not self.profile.read_only and not self.working)
        has_row = self.records.currentIndex().isValid()
        self.add_row.setEnabled(can_write)
        self.edit_cell.setEnabled(can_write and has_row and bool(self.primary))
        self.delete_row.setEnabled(can_write and has_row and bool(self.primary))
        self.prev.setEnabled(bool(table and self.offset and not self.working))
        self.next.setEnabled(bool(table and self.more_rows and not self.working))
        self.run_button.setEnabled(bool(self.session and not self.working))
        self.objects.setEnabled(not self.working)
        self.schemas.setEnabled(not self.working)
        self.pages.tabBar().setEnabled(not self.working)

    def change_row(self, action):
        session, ref = self.session, self.table
        if self.working or not session or not ref or ref.view:
            return
        selected = self.records.currentIndex()
        original = None
        values = {}
        if action == "insert":
            values = self._new_record()
            if values is None:
                return
        else:
            if not self.primary or not selected.isValid():
                return
            original = dict(zip(self.record_model.columns, self.record_model.rows[selected.row()]))
            if action == "delete":
                if QtWidgets.QMessageBox.question(self, ref.name, "Удалить выбранную строку?") != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            else:
                name = self.record_model.columns[selected.column()]
                current = original[name]
                initial = "" if current is None else current.hex() if isinstance(current, bytes) else str(current)
                dialog = QtWidgets.QInputDialog(self)
                dialog.setWindowTitle(ref.name)
                dialog.setLabelText(name + " · \\N для SQL NULL")
                dialog.setTextValue(initial if current is not None else r"\N")
                if not dialog.exec():
                    return
                value = dialog.textValue()
                values[name] = None if value == r"\N" else value
        self._job(lambda: session.mutate(ref, action, values, original), lambda _: self._load_page())

    def _new_record(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Новая строка")
        layout = QtWidgets.QVBoxLayout(dialog)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        fields = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(fields)
        inputs = []
        for name in self.record_model.columns:
            mode = QtWidgets.QComboBox()
            mode.addItems(["DEFAULT", "Значение", "NULL"])
            edit = QtWidgets.QLineEdit()
            edit.setEnabled(False)
            mode.currentIndexChanged.connect(lambda index, field=edit: field.setEnabled(index == 1))
            row = QtWidgets.QHBoxLayout()
            row.addWidget(mode)
            row.addWidget(edit, 1)
            form.addRow(name, row)
            inputs.append((name, mode, edit))
        scroll.setWidget(fields)
        layout.addWidget(scroll)
        actions = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        dialog.resize(480, min(560, 140 + 42 * len(inputs)))
        if not dialog.exec():
            return None
        return {name: edit.text() if mode.currentIndex() == 1 else None
                for name, mode, edit in inputs if mode.currentIndex()}

    def create_table(self):
        if not self.session or self.working:
            self.status.setText("Выберите подключение")
            return
        if self.profile.read_only:
            self.status.setText("Подключение доступно только для чтения")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Создать таблицу")
        layout = QtWidgets.QVBoxLayout(dialog)
        name = QtWidgets.QLineEdit()
        name.setPlaceholderText("Имя таблицы")
        layout.addWidget(name)
        columns = QtWidgets.QTableWidget(0, 4)
        columns.setHorizontalHeaderLabels(["Колонка", "Тип", "PK", "NOT NULL"])
        columns.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(columns)
        feedback = QtWidgets.QLabel()
        layout.addWidget(feedback)

        def add_column():
            row = columns.rowCount()
            columns.insertRow(row)
            columns.setItem(row, 0, QtWidgets.QTableWidgetItem("id" if row == 0 else ""))
            kind = QtWidgets.QComboBox()
            kind.addItems(["INTEGER", "BIGINT", "VARCHAR", "TEXT", "FLOAT", "BOOLEAN", "TIMESTAMP"])
            columns.setCellWidget(row, 1, kind)
            for index in (2, 3):
                flag = QtWidgets.QTableWidgetItem()
                flag.setCheckState(QtCore.Qt.CheckState.Checked if row == 0 else QtCore.Qt.CheckState.Unchecked)
                columns.setItem(row, index, flag)

        def preview():
            fields = [(columns.item(row, 0).text().strip(), columns.cellWidget(row, 1).currentText(),
                       columns.item(row, 2).checkState() == QtCore.Qt.CheckState.Checked,
                       columns.item(row, 3).checkState() == QtCore.Qt.CheckState.Checked)
                      for row in range(columns.rowCount())]
            try:
                source = self.session.create_table_sql(self.schemas.currentData(), name.text().strip(), fields)
            except (ValueError, TypeError) as exc:
                feedback.setText(str(exc))
                return
            dialog.accept()
            self.pages.setCurrentIndex(0)
            cursor = self.sql.textCursor()
            if self.sql.toPlainText().strip() and not cursor.hasSelection():
                cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                cursor.insertText("\n\n")
            start = cursor.selectionStart()
            cursor.insertText(source)
            cursor.setPosition(start, QtGui.QTextCursor.MoveMode.KeepAnchor)
            self.sql.setTextCursor(cursor)
            self.sql.setFocus()

        actions = QtWidgets.QHBoxLayout()
        button(actions, "+ Колонка", add_column)
        button(actions, "Убрать колонку", lambda: columns.removeRow(columns.currentRow()))
        actions.addStretch(1)
        button(actions, "Открыть SQL", preview)
        layout.addLayout(actions)
        add_column()
        dialog.resize(560, 330)
        dialog.exec()

    def bind(self):
        if not self.profile:
            return
        try:
            if self.panel.bound == self.profile:
                if QtWidgets.QMessageBox.question(self, "Database", "Снять привязку к проекту?") != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
                self.panel.bind(None)
                self.runtime.setChecked(False)
                self.status.setText("Привязка снята")
            else:
                self.panel.bind(self.profile)
                self.runtime.setChecked(False)
                self.status.setText("Подключение привязано к проекту")
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def _set_runtime(self, enabled):
        try:
            self.panel.set_runtime(self.profile, enabled, self.session_password)
        except ValueError as exc:
            self.status.setText(str(exc))
            with QtCore.QSignalBlocker(self.runtime):
                self.runtime.setChecked(False)

    @QtCore.pyqtSlot()
    def _sync_runtime(self):
        with QtCore.QSignalBlocker(self.runtime):
            self.runtime.setChecked(self.profile is not None and self.panel.runtime_profile == self.profile)

    def load_sql(self, source):
        if source != self.sql.toPlainText() and not self.confirm_close():
            return False
        self.sql.setPlainText(source)
        self.pages.setCurrentIndex(0)
        self.sql.setFocus()
        return True

    def confirm_close(self):
        if not self.sql.document().isModified() or not self.sql.toPlainText().strip():
            return True
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.question(self, "SQL", "Сохранить изменения в SQL?",
                                                buttons.Yes | buttons.No | buttons.Cancel, buttons.Yes)
        if answer == buttons.Yes:
            return self.save_sql()
        return answer == buttons.No

    def save_sql(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить SQL", str(self.panel.root or ""), "SQL (*.sql);;Все файлы (*)"
        )
        if not path:
            return False
        saved = self._save_bytes(path, self.sql.toPlainText().encode("utf-8"))
        if saved:
            self.sql.document().setModified(False)
        return saved

    def _save_bytes(self, path, payload):
        target = QtCore.QSaveFile(path)
        if not target.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
            self.status.setText(target.errorString())
        elif target.write(payload) != len(payload):
            target.cancelWriting()
            self.status.setText(target.errorString())
        elif not target.commit():
            self.status.setText(target.errorString())
        else:
            self.status.setText(Path(path).name)
            return True
        return False

    def run_sql(self, checked=False, *, plan=False):
        if self.working:
            return
        if not self.session:
            self.status.setText("Выберите подключение")
            return
        cursor = self.sql.textCursor()
        source = cursor.selectedText().replace("\u2029", "\n") if cursor.hasSelection() else self.sql.toPlainText()
        session, autocommit = self.session, self.autocommit.isChecked()
        self._clear_query_results()

        def ready(result):
            with QtCore.QSignalBlocker(self.result_selector):
                for preview in result.result_sets:
                    suffix = "+" if preview.truncated else ""
                    label = f"SQL {preview.number} · {len(preview.rows)}{suffix} строк"
                    self.result_selector.addItem(label, preview)
                self.result_selector.setCurrentIndex(self.result_selector.count() - 1)
            self.result_selector.setVisible(self.result_selector.count() > 1)
            self._show_selected_result()
            self.execution_model.set_data(("Запрос", "мс", "Строки"), result.details)
            self.output_tabs.setCurrentIndex(0 if result.result_sets else 1)
            count = len(result.rows) if result.columns else result.affected
            suffix = "+" if result.truncated else ""
            notice = f"{result.elapsed_ms} мс" + (f" · {count}{suffix} строк" if count >= 0 else "")
            self.status.setText(notice)
            if result.objects_changed:
                self.refresh_catalog(notice=notice)

        self._job(lambda: session.run(source, plan=plan, autocommit=autocommit), ready)

    def _clear_query_results(self):
        with QtCore.QSignalBlocker(self.result_selector):
            self.result_selector.clear()
        self.result_selector.hide()
        self.csv_button.setEnabled(False)
        self.result_model.set_data((), [])
        self.execution_model.set_data((), [])

    def _show_selected_result(self):
        result = self.result_selector.currentData()
        self.csv_button.setEnabled(result is not None)
        if result is not None:
            self.result_model.set_data(result.columns, result.rows)

    def export_csv(self):
        result = self.result_selector.currentData()
        if result is None:
            return
        name = f"query-{result.number}.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Экспорт результата", str((self.panel.root or Path.cwd()) / name), "CSV (*.csv)"
        )
        if path:
            if not Path(path).suffix:
                path += ".csv"
            self._save_bytes(path, csv_preview(result.columns, result.rows))

    def _jump_to_result(self, index):
        number = self.execution_model.rows[index.row()][0]
        for row in range(self.result_selector.count()):
            if self.result_selector.itemData(row).number == number:
                self.result_selector.setCurrentIndex(row)
                self.output_tabs.setCurrentIndex(0)
                return

    def _show_result_cell(self, index):
        value = self.result_model.rows[index.row()][index.column()]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.result_model.columns[index.column()])
        layout = QtWidgets.QVBoxLayout(dialog)
        contents = QtWidgets.QPlainTextEdit()
        contents.setReadOnly(True)
        contents.setPlainText("NULL" if value is None else value.hex() if isinstance(value, bytes) else str(value))
        layout.addWidget(contents)
        dialog.resize(700, 440)
        dialog.exec()

    def _templates(self):
        if not self.profile:
            self.status.setText("Выберите подключение")
            return
        kind = self.profile.kind
        templates = {
            "Создать таблицу": "CREATE TABLE new_table (\n    id INTEGER PRIMARY KEY,\n    name VARCHAR(255) NOT NULL\n);",
            "Добавить колонку": "ALTER TABLE table_name ADD column_name VARCHAR(255);",
            "Создать индекс": "CREATE INDEX index_name ON table_name (column_name);",
            "Создать представление": "CREATE VIEW view_name AS\nSELECT column_name FROM table_name;",
        }
        if kind != "sqlite":
            templates["Активные запросы"] = {
                "postgresql": "SELECT * FROM pg_stat_activity WHERE state = 'active';",
                "mysql": "SHOW FULL PROCESSLIST;",
                "mssql": "SELECT * FROM sys.dm_exec_requests;",
            }[kind]
            templates["Создать базу"] = "CREATE DATABASE new_database;"
            templates["Выдать права"] = {
                "postgresql": "GRANT SELECT ON TABLE table_name TO role_name;",
                "mysql": "GRANT SELECT ON database_name.table_name TO 'user_name'@'localhost';",
                "mssql": "GRANT SELECT ON OBJECT::dbo.table_name TO user_name;",
            }[kind]
        else:
            templates["Проверка целостности"] = "PRAGMA integrity_check;"
            templates["Обслуживание"] = "VACUUM;"
        command, accepted = QtWidgets.QInputDialog.getItem(self, "DDL", "Операция", list(templates), editable=False)
        if accepted:
            self.pages.setCurrentIndex(0)
            cursor = self.sql.textCursor()
            cursor.insertText(templates[command])
            self.sql.setTextCursor(cursor)
            self.sql.setFocus()
