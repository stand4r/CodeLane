"""CodeLane widgets/SettingsWidget.py: application module and reusable component."""
from core.activity_log import record_action
from design.icons import set_line_icon
from PyQt6 import QtWidgets, QtCore, QtGui
from utils.settings_manager import get_settings_manager


from utils.settings_manager import DEFAULT_SETTINGS
from design.themes import THEMES, THEME_NAMES
from services.ai_providers import PROVIDERS, get_provider, provider_choices
from services.ai_service import REASONING_HINTS, REASONING_LABELS, TEACH_STRICTNESS_HINTS
from services.background import run_async

DEFAULTS = DEFAULT_SETTINGS

MCP_EXAMPLE = (
    '[{"name": "files", "transport": "stdio", "command": "npx",\n'
    '  "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},\n'
    ' {"name": "git", "transport": "stdio", "command": "uvx",\n'
    '  "args": ["mcp-server-git", "--repository", "."]},\n'
    ' {"name": "remote", "transport": "http", "url": "http://localhost:3001/mcp",\n'
    '  "headers": {"Authorization": "Bearer your-token"}}]'
)


class _MCPProgressBridge(QtCore.QObject):
    """Relays per-server progress lines from the worker thread to the GUI."""

    progress = QtCore.pyqtSignal(str)


def check_mcp_servers(servers, report):
    """Probe MCP servers (runs on the shared Python pool, never on a Qt thread)."""
    from services.mcp_client import MCPClient

    lines = []
    for config in servers:
        if not config.enabled:
            lines.append(f"– {config.label}: отключён")
            continue
        report(f"Проверяется {config.label}…")
        client = MCPClient(config, timeout=8)
        try:
            tools = client.connect()
            names = ", ".join(tool.name for tool in tools[:6])
            suffix = f" · {names}" if names else ""
            if len(tools) > 6:
                suffix += f" · +{len(tools) - 6}"
            server = client.server_info.get("name") or config.label
            lines.append(f"✓ {server}: {len(tools)} инструментов{suffix}")
        except Exception as exc:  # noqa: BLE001 - show per-server failures
            lines.append(f"✗ {config.label}: {exc}")
        finally:
            client.close()
    return lines

class SettingsWidget(QtWidgets.QWidget):
    """Full CodeLane workbench settings editor.

    Settings are saved and applied from the footer.
    """
    def __init__(self, settings=None, pathSettings=None, parent=None):
        super().__init__(parent)
        self.path_settings = pathSettings
        self.settings_manager = get_settings_manager()
        self.main_window = parent
        self.controls = {}
        self._mcp_test_worker = None
        self._building = True
        self._build()
        self._building = False

    def _value(self, key):
        return self.settings_manager.get_setting(key, DEFAULTS[key])

    def _build(self):
        self.setObjectName("settingsWorkbench")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)

        title = QtWidgets.QLabel("Настройки")
        title.setObjectName("settingsTitle")
        root.addWidget(title)
        self._compact_navigation = QtWidgets.QComboBox()
        self._compact_navigation.setObjectName("settingsCompactNavigation")
        self._compact_navigation.setAccessibleName("Раздел настроек")
        self._compact_navigation.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        root.addWidget(self._compact_navigation)
        self._compact_navigation.hide()

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 1)
        self._navigation = QtWidgets.QListWidget()
        self._navigation.setObjectName("settingsNavigation")
        self._navigation.setAccessibleName("Разделы настроек")
        self._navigation.setFixedWidth(188)
        self._navigation.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.addWidget(self._navigation)
        self._pages = QtWidgets.QStackedWidget()
        self._pages.setObjectName("settingsPages")
        body.addWidget(self._pages, 1)

        groups = (
            ("Рабочая среда", (
                ("Оформление", "Тема, масштаб, шрифты и размеры панелей.", self._appearance_page),
                ("Редактор", "Текст, подсветка и автосохранение.", self._editor_page),
                ("Терминал", "Размер панели и текста терминала.", self._terminal_page),
            )),
            ("Проекты и запуск", (
                ("Проекты и сеанс", "Недавние проекты и восстановление работы после запуска.", self._projects_page),
                ("Компиляторы", "Компиляторы, интерпретаторы и локальные СУБД.", self._toolchains_page),
            )),
            ("Инструменты", (
                ("ИИ-агент", "Подключение модели, ответы и разрешения.", self._ai_page),
                ("Браузер", "Поисковик, профили и User-Agent.", self._browser_page),
                ("Расширения", "Дополнительные инструменты CodeLane.", self._extensions_page),
            )),
            ("Приложение", (
                ("Система", "Версия, обновления и интеграция с Windows.", self._system_page),
            )),
        )
        self._nav_items = []
        self._page_builders = {}
        for group, sections in groups:
            heading = QtWidgets.QListWidgetItem(group.upper())
            heading.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self._navigation.addItem(heading)
            for label, description, build in sections:
                index = self._pages.count()
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, index)
                self._nav_items.append(item)
                self._navigation.addItem(item)
                self._compact_navigation.addItem(label, index)
                self._page_builders[index] = (label, description, build)
                self._pages.addWidget(QtWidgets.QWidget())
        self._navigation.currentItemChanged.connect(
            lambda item, _: self._select_page(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None
        )
        self._compact_navigation.currentIndexChanged.connect(self._select_page)
        self._select_page(0)

        self.footer = QtWidgets.QFrame()
        self.footer.setObjectName("settingsFooter")
        buttons = QtWidgets.QHBoxLayout(self.footer)
        buttons.setContentsMargins(12, 8, 10, 8)
        self._apply_status = QtWidgets.QLabel("Настройки сохраняются после применения")
        self._apply_status.setObjectName("settingsHint")
        self._apply_status.setWordWrap(True)
        buttons.addWidget(self._apply_status, 1)
        self.apply_button = QtWidgets.QPushButton("Применить")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(lambda: self.apply_settings(save_file=True))
        buttons.addWidget(self.apply_button)
        root.addWidget(self.footer)

    def _scroll_page(self, label, description, page):
        for combo in page.findChildren(QtWidgets.QComboBox):
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("settingsPageScroll")
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        content.setObjectName("settingsPageContent")
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(8)
        heading = QtWidgets.QLabel(label)
        heading.setObjectName("settingsPageTitle")
        layout.addWidget(heading)
        hint = QtWidgets.QLabel(description)
        hint.setWordWrap(True)
        hint.setObjectName("settingsHint")
        layout.addWidget(hint)
        layout.addWidget(page)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _select_page(self, index):
        if index is None or not 0 <= index < self._pages.count():
            return
        with QtCore.QSignalBlocker(self._navigation), QtCore.QSignalBlocker(self._compact_navigation):
            spec = self._page_builders.get(index)
            if spec is not None:
                label, description, build = spec
                page = self._scroll_page(label, description, build())
                placeholder = self._pages.widget(index)
                self._pages.removeWidget(placeholder)
                placeholder.deleteLater()
                self._pages.insertWidget(index, page)
                del self._page_builders[index]
            self._pages.setCurrentIndex(index)
            self._navigation.setCurrentItem(self._nav_items[index])
            self._compact_navigation.setCurrentIndex(index)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = event.size().width() < 720
        self._navigation.setVisible(not compact)
        self._compact_navigation.setVisible(compact)

    @staticmethod
    def _section(layout, title):
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("settingsSectionTitle")
        layout.addRow(heading)

    def _page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)
        layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return page, layout

    def _combo(self, key, values):
        w = QtWidgets.QComboBox()
        for v in values: w.addItem(v)
        w.setCurrentText(str(self._value(key)))
        self.controls[key] = w
        w.currentTextChanged.connect(self._live_apply)
        return w

    def _spin(self, key, lo, hi):
        w = QtWidgets.QSpinBox(); w.setRange(lo, hi); w.setValue(int(self._value(key)))
        self.controls[key] = w
        w.valueChanged.connect(self._live_apply)
        return w

    def _check(self, key, text):
        w = QtWidgets.QCheckBox(text); w.setChecked(bool(self._value(key)))
        self.controls[key] = w
        w.stateChanged.connect(self._live_apply)
        return w

    def _color(self, key):
        row = QtWidgets.QWidget(); lay = QtWidgets.QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
        edit = QtWidgets.QLineEdit(str(self._value(key)))
        pick = QtWidgets.QPushButton('Выбрать')
        pick.setFixedWidth(82)
        pick.clicked.connect(lambda: self._pick(edit))
        lay.addWidget(edit, 1); lay.addWidget(pick)
        self.controls[key] = edit
        return row

    def _pick(self, edit):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(edit.text()), self, 'Выбор цвета')
        if c.isValid(): edit.setText(c.name())

    def _theme_combo(self):
        w = QtWidgets.QComboBox()
        for key, label in THEME_NAMES.items():
            w.addItem(label, key)
        current = str(self._value('theme'))
        index = max(0, w.findData(current))
        w.setCurrentIndex(index)
        self.controls['theme'] = w
        w.currentIndexChanged.connect(self._theme_changed)
        return w

    def _theme_changed(self, index):
        key = self.controls['theme'].itemData(index)
        preset = THEMES.get(key)
        if not preset:
            return
        for name, value in preset.items():
            control = self.controls.get(name)
            if isinstance(control, QtWidgets.QLineEdit):
                control.setText(value)

    def _appearance_page(self):
        page, l = self._page()
        self._section(l, 'Тема и масштаб')
        l.addRow('Тема', self._theme_combo())
        # Accessibility: scales the entire workbench (icons, rows, menus, text),
        # not just the font — useful for users with low vision.
        l.addRow('Масштаб интерфейса', self._scale_row())
        self._section(l, "Шрифт")
        # Installed families only: a name the system does not have would
        # silently fall back to the Qt default and look like a broken setting.
        from app.styling import is_font_available, resolve_ui_font, UI_FONT_CANDIDATES
        font_choices = [('', f'По умолчанию ({resolve_ui_font()})')]
        for family in UI_FONT_CANDIDATES:
            if is_font_available(family):
                font_choices.append((family, family))
        current_font = str(self._value('ui_font') or '')
        if current_font and current_font not in [c[0] for c in font_choices]:
            font_choices.append((current_font, current_font))
        font_combo = QtWidgets.QComboBox()
        for value, label in font_choices:
            font_combo.addItem(label, value)
        idx = font_combo.findData(current_font)
        font_combo.setCurrentIndex(max(0, idx))
        font_combo.setToolTip('Шрифт всего интерфейса. «По умолчанию» выбирает '
                              'наиболее подходящий установленный шрифт.')
        self.controls['ui_font'] = font_combo
        l.addRow('Шрифт интерфейса', font_combo)
        l.addRow('Размер текста интерфейса', self._spin('ui_font_size', 12, 15))
        self._section(l, 'Размеры')
        l.addRow('Ширина боковой панели', self._spin('sidebar_width', 220, 500))
        l.addRow('Высота вкладок', self._spin('tab_height', 28, 48))
        self._section(l, "Цвета темы")
        for key, label in [
            ('accent_color','Акцент'), ('app_background','Фон приложения'),
            ('editor_background','Фон редактора'), ('sidebar_background','Фон боковой панели'),
            ('panel_background','Фон нижней панели'), ('input_background','Фон полей'),
            ('hover_background','При наведении'), ('text_primary','Основной текст'), ('text_secondary','Вторичный текст')]:
            l.addRow(label, self._color(key))
        return page

    def _scale_row(self):
        """Edit the pending interface scale."""
        from app.ui_scale import normalize_scale
        row = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        line = QtWidgets.QHBoxLayout()
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(75, 200)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setTickInterval(25)
        slider.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        value = normalize_scale(self._value('ui_scale_percent'))
        slider.setValue(value)
        label = QtWidgets.QLabel(f'{value}%')
        label.setMinimumWidth(48)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(slider, 1)
        line.addWidget(label)
        lay.addLayout(line)

        hint = QtWidgets.QLabel(
            '100% — базовый масштаб для текущего монитора. Новый масштаб применяется после сохранения. '
            'Горячие клавиши: Ctrl+Alt+= / Ctrl+Alt+− / Ctrl+Alt+0.'
        )
        hint.setWordWrap(True)
        hint.setObjectName('settingsHint')
        lay.addWidget(hint)

        slider.valueChanged.connect(lambda value: label.setText(f'{value}%'))
        self.controls['ui_scale_percent'] = slider
        return row

    def _editor_page(self):
        page, l = self._page()
        l.addRow('Шрифт редактора', self._combo('editor_font', ['Cascadia Code','Consolas','JetBrains Mono','Courier New']))
        l.addRow('Размер редактора', self._spin('editor_font_size', 9, 24))
        l.addRow('', self._check('show_line_numbers','Показывать номера строк'))
        l.addRow('', self._check('syntax_highlighting','Подсветка синтаксиса'))
        l.addRow('', self._check('word_wrap','Перенос строк'))
        self._section(l, 'Сохранение')
        l.addRow('Автосохранение, сек.', self._spin('auto_save_interval', 0, 3600))
        l.addRow('', self._check('format_on_save', 'Форматировать Python при сохранении'))
        return page

    def _terminal_page(self):
        page, l = self._page()
        l.addRow('Высота панели терминала', self._spin('terminal_panel_height', 120, 800))
        return page

    def _toolchains_page(self):
        page, l = self._page()
        hint = QtWidgets.QLabel('Пустое поле означает автоматический поиск программы в PATH.')
        hint.setWordWrap(True)
        hint.setObjectName('settingsHint')
        l.addRow(hint)
        languages = [
            ('python', 'Python', 'python'), ('javascript', 'JavaScript', 'node'), ('typescript', 'TypeScript', 'tsc'),
            ('go', 'Go', 'go'), ('rust', 'Rust', 'rustc'), ('c', 'C', 'gcc'), ('cpp', 'C++', 'g++'),
            ('java', 'Java', 'javac'), ('csharp', 'C#', 'dotnet'), ('php', 'PHP', 'php'), ('ruby', 'Ruby', 'ruby'),
            ('lua', 'Lua', 'lua'), ('shell', 'Shell', 'bash')
        ]
        toolchains = self.settings_manager.get_setting('toolchains', {}) or {}
        for key, label, placeholder in languages:
            edit = QtWidgets.QLineEdit(str((toolchains.get(key) or {}).get('command', '')))
            edit.setPlaceholderText(f'Например: {placeholder}')
            self.controls[f'toolchain_{key}'] = edit
            l.addRow(label, edit)

        detect_btn = QtWidgets.QPushButton('Автопоиск инструментов в системе')
        set_line_icon(detect_btn, 'search')
        detect_btn.setStyleSheet("""
            QPushButton { background-color: #0078D4; color: #FFFFFF; font-weight: 600; padding: 6px 14px; border-radius: 6px; }
            QPushButton:hover { background-color: #1A8AD8; }
        """)
        status_lbl = QtWidgets.QLabel('')
        status_lbl.setWordWrap(True)
        status_lbl.setStyleSheet('color: #75BEFF; font-size: 11px;')

        def _do_detect():
            from utils.toolchain_detector import detect_all_toolchains
            results = detect_all_toolchains()
            found_names = []
            for k, meta in results.items():
                if meta['found'] and meta['path']:
                    ctrl = self.controls.get(f'toolchain_{k}')
                    if ctrl:
                        ctrl.setText(meta['path'])
                        found_names.append(meta['name'].split()[0])
            count = len(found_names)
            msg = f'✓ Обнаружено инструментов: {count} ({", ".join(found_names)})' if count else 'Инструменты не найдены в стандартных путях'
            status_lbl.setText(msg)
            self._live_apply()

        detect_btn.clicked.connect(_do_detect)
        l.addRow('', detect_btn)
        l.addRow('', status_lbl)
        from core.code_analysis.registry import get_language_registry

        self._section(l, "Языковые серверы (LSP)")
        languages = QtWidgets.QComboBox()
        paths = QtWidgets.QStackedWidget()
        specs = sorted(get_language_registry().all_specs(), key=lambda spec: spec.display_name.casefold())
        for spec in specs:
            if not spec.lsp_candidates:
                continue
            key = f"lsp_{spec.id}_path"
            field = QtWidgets.QLineEdit(str(self.settings_manager.get_setting(key, "") or ""))
            field.setPlaceholderText(f"Автопоиск: {spec.lsp_candidates[0]}")
            field.setToolTip(spec.install_hint)
            field.textEdited.connect(self._live_apply)
            self.controls[key] = field
            languages.addItem(spec.display_name)
            paths.addWidget(field)
        languages.currentIndexChanged.connect(paths.setCurrentIndex)
        l.addRow("Язык", languages)
        l.addRow("Сервер", paths)
        database = getattr(self.main_window, "database_manager", None)
        if database is not None:
            self._section(l, "Локальные СУБД")
            self.database_installations = QtWidgets.QLabel()
            self.database_installations.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.database_installations.setWordWrap(True)
            l.addRow(self.database_installations)
            self.database_scan = QtWidgets.QPushButton("Обновить список СУБД")
            self.database_scan.clicked.connect(database.discover_local_installations)
            l.addRow("", self.database_scan)
            database.local_installations_changed.connect(self._show_database_installations)
            self._show_database_installations()
        return page

    @QtCore.pyqtSlot()
    def _show_database_installations(self):
        from core.database_workspace import ENGINES

        database = self.main_window.database_manager
        entries = []
        for profile in database.local_installations.values():
            endpoint = f"{profile.host}:{profile.port}" if profile.port else profile.host
            entries.append(f"{ENGINES[profile.kind][0]} · {profile.name} · {endpoint}")
        if database.local_scan_running:
            entries.append("Поиск…")
        elif database.local_scan_error:
            entries.append(database.local_scan_error)
        self.database_installations.setText("\n".join(entries) or "Локальные СУБД не найдены")
        self.database_scan.setEnabled(not database.local_scan_running)


    def _projects_page(self):
        page, l = self._page()
        self._section(l, "Проекты")
        self._section(l, 'Сеанс приложения')
        l.addRow('', self._check('restore_session', 'Восстанавливать открытые файлы при запуске'))
        l.addRow('', self._check('show_welcome', 'Показывать стартовую страницу'))
        return page

    def _line(self, key, placeholder):
        w = QtWidgets.QLineEdit(str(self._value(key)))
        w.setPlaceholderText(placeholder)
        self.controls[key] = w
        w.textChanged.connect(self._live_apply)
        return w

    def _extensions_page(self):
        from utils.settings_manager import BUILTIN_EXTENSIONS
        from widgets.ExtensionsWidget import ExtensionsWidget

        page, layout = self._page()
        self._section(layout, "Встроенные расширения")
        for key, (name, description) in BUILTIN_EXTENSIONS.items():
            toggle = self._check(f"extension_{key}", name)
            toggle.setToolTip(description)
            layout.addRow(toggle)
            hint = QtWidgets.QLabel(description)
            hint.setWordWrap(True)
            hint.setObjectName("settingsHint")
            layout.addRow(hint)

        if self.main_window is not None and hasattr(self.main_window, "extension_manager"):
            self._section(layout, "Установленные расширения")
            manager = ExtensionsWidget(self.main_window.extension_manager, page)
            self.installed_extensions = manager
            manager.setMinimumHeight(260)
            layout.addRow(manager)
        return page

    def open_extensions_page(self):
        for item in self._nav_items:
            if item.text() == "Расширения":
                self._select_page(item.data(QtCore.Qt.ItemDataRole.UserRole))
                break

    def _system_page(self):
        """Updates from GitHub releases and Windows Explorer integration."""
        from core import shell_integration
        from core.app_version import current_version
        from services.updater import GITHUB_REPO

        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QtWidgets.QLabel('Обновления CodeLane')
        title.setObjectName('settingsSectionTitle')
        layout.addWidget(title)
        version = QtWidgets.QLabel(
            f'Установленная версия: v{current_version()} · github.com/{GITHUB_REPO}')
        version.setObjectName('settingsHint')
        layout.addWidget(version)
        layout.addWidget(self._check(
            'update_auto_check', 'Проверять обновления автоматически при запуске'))
        check_now = QtWidgets.QPushButton('Проверить обновления…')
        check_now.clicked.connect(
            lambda: self.main_window.action_check_updates() if self.main_window else None)
        layout.addWidget(check_now, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        updates_hint = QtWidgets.QLabel(
            'Обновления берутся из GitHub-релизов stand4r/PyScribe. '
            'Для установленной версии скачивается установщик, для запуска из '
            'исходников — git pull, после чего CodeLane перезапускается.')
        updates_hint.setWordWrap(True)
        updates_hint.setObjectName('settingsHint')
        layout.addWidget(updates_hint)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        layout.addWidget(line)

        explorer_title = QtWidgets.QLabel('Проводник Windows')
        explorer_title.setObjectName('settingsSectionTitle')
        layout.addWidget(explorer_title)
        self._shell_box = QtWidgets.QCheckBox(
            'Пункт «Открыть в CodeLane» в проводнике')
        self._shell_box.setChecked(shell_integration.is_registered())
        self._shell_box.toggled.connect(self._toggle_shell_integration)
        layout.addWidget(self._shell_box)
        self._shell_status = QtWidgets.QLabel(
            'Интеграция включена.' if shell_integration.is_registered()
            else 'Интеграция выключена.')
        self._shell_status.setObjectName('settingsHint')
        layout.addWidget(self._shell_status)
        explorer_hint = QtWidgets.QLabel(
            'CodeLane также появится в системном окне «Открыть с помощью». '
            'Файлы открываются в редакторе, папки — как проект. Записи создаются '
            'только для текущего пользователя и не требуют прав администратора.')
        explorer_hint.setWordWrap(True)
        explorer_hint.setObjectName('settingsHint')
        layout.addWidget(explorer_hint)
        layout.addStretch()
        return page

    def _toggle_shell_integration(self, checked: bool):
        from core import shell_integration
        if checked:
            ok, errors = shell_integration.register()
            self._shell_status.setText(
                'Готово: CodeLane добавлен в меню проводника.' if ok
                else 'Не удалось добавить записи: ' + '; '.join(errors[:2]))
        else:
            ok, errors = shell_integration.unregister()
            self._shell_status.setText(
                'Записи удалены.' if ok
                else 'Не удалось удалить записи: ' + '; '.join(errors[:2]))
        if not ok:
            self._shell_box.blockSignals(True)
            self._shell_box.setChecked(not checked)
            self._shell_box.blockSignals(False)

    def _live_apply(self, *args):
        # Ordinary controls are edited locally and committed only by Apply/Save.
        # This prevents opening Settings and touching a control from causing a
        # cascade of unrelated persistence changes.
        return

    def _browser_page(self):
        from services.browser_settings import BUILTIN_PROFILE_IDS, SEARCH_ENGINES, default_profile, profiles

        page, form = self._page()
        engine = QtWidgets.QComboBox()
        for key, (label, _, _) in SEARCH_ENGINES.items():
            engine.addItem(label, key)
        engine.setCurrentIndex(max(0, engine.findData(self._value("browser_search_engine"))))
        self.controls["browser_search_engine"] = engine
        self._section(form, "Поиск")
        form.addRow("Основной поиск", engine)

        default = QtWidgets.QComboBox()
        self.controls["browser_default_profile"] = default
        self._section(form, "Профили")
        form.addRow("Профиль по умолчанию", default)
        hint = QtWidgets.QLabel(
            "Приватные профили хранят данные сайтов только в памяти. Постоянные профили "
            "сохраняют cookies и данные сайтов отдельно друг от друга. "
            "Выбор поиска и профиля по умолчанию вступит в силу после «Применить»."
        )
        hint.setObjectName("settingsHint")
        hint.setWordWrap(True)
        form.addRow(hint)

        profile_list = QtWidgets.QListWidget()
        profile_list.setMinimumHeight(120)
        form.addRow(profile_list)
        actions = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Добавить")
        edit = QtWidgets.QPushButton("Изменить")
        remove = QtWidgets.QPushButton("Убрать из списка")
        remove.setToolTip("Убрать профиль из меню, сохранив его локальные данные")
        for button in (add, edit, remove):
            actions.addWidget(button)
        actions.addStretch(1)
        form.addRow(actions)

        def refresh():
            with QtCore.QSignalBlocker(default):
                default.clear()
                profile_list.clear()
                for profile in profiles():
                    default.addItem(profile["name"], profile["id"])
                    if profile["id"] not in BUILTIN_PROFILE_IDS:
                        mode = "постоянный" if profile["persistent"] else "приватный"
                        item = QtWidgets.QListWidgetItem(f'{profile["name"]} · {mode}')
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, profile["id"])
                        profile_list.addItem(item)
                default.setCurrentIndex(default.findData(default_profile()["id"]))
            edit.setEnabled(False)
            remove.setEnabled(False)

        def selected():
            item = profile_list.currentItem()
            key = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
            return next((profile for profile in profiles() if profile["id"] == key), None)

        def change(profile=None):
            updated = self._edit_browser_profile(profile)
            if updated is None:
                return
            entries = [entry for entry in profiles()
                       if entry["id"] not in BUILTIN_PROFILE_IDS and entry["id"] != updated["id"]]
            if len(entries) >= 40:
                QtWidgets.QMessageBox.information(self, "Браузер", "Доступно до 40 профилей.")
                return
            entries.append(updated)
            self._save_browser_profiles(entries)
            refresh()

        def delete():
            profile = selected()
            if profile is not None:
                self._save_browser_profiles([entry for entry in profiles()
                                             if entry["id"] not in BUILTIN_PROFILE_IDS and entry["id"] != profile["id"]])
                refresh()

        profile_list.currentItemChanged.connect(
            lambda item, _previous: (edit.setEnabled(item is not None), remove.setEnabled(item is not None))
        )
        add.clicked.connect(lambda: change())
        edit.clicked.connect(lambda: change(selected()) if selected() else None)
        remove.clicked.connect(delete)
        refresh()
        engine.currentIndexChanged.connect(self._live_apply)
        default.currentIndexChanged.connect(self._live_apply)
        return page

    def _save_browser_profiles(self, entries):
        from services.browser_settings import BUILTIN_PROFILE_IDS

        manager = self.settings_manager
        previous = manager.get_setting("browser_profiles", [])
        previous_default = manager.get_setting("browser_default_profile", "standard")
        manager.settings["browser_profiles"] = entries
        if previous_default not in BUILTIN_PROFILE_IDS and not any(entry["id"] == previous_default for entry in entries):
            manager.settings["browser_default_profile"] = "standard"
        if not manager.save_settings():
            manager.settings["browser_profiles"] = previous
            manager.settings["browser_default_profile"] = previous_default
            QtWidgets.QMessageBox.warning(self, "Браузер", "Не удалось сохранить профили.")

    def _edit_browser_profile(self, profile):
        from uuid import uuid4
        from services.browser_settings import UA_MODES, valid_agent

        profile = profile or {}
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Профиль браузера")
        form = QtWidgets.QFormLayout(dialog)
        name = QtWidgets.QLineEdit(profile.get("name", ""))
        name.setMaxLength(48)
        form.addRow("Название", name)
        persistent = QtWidgets.QCheckBox("Хранить данные сайтов и сеансы на диске")
        persistent.setChecked(profile.get("persistent", False))
        form.addRow("Режим профиля", persistent)
        mode = QtWidgets.QComboBox()
        for key, label in UA_MODES.items():
            mode.addItem(label, key)
        mode.setCurrentIndex(mode.findData(profile.get("ua_mode", "native")))
        form.addRow("User-Agent", mode)
        agent = QtWidgets.QLineEdit(profile.get("ua", ""))
        agent.setMaxLength(512)
        agent.setEnabled(mode.currentData() == "custom")
        mode.currentIndexChanged.connect(lambda _: agent.setEnabled(mode.currentData() == "custom"))
        form.addRow("Свой User-Agent", agent)
        hint = QtWidgets.QLabel("User-Agent меняет строку, которую браузер сообщает сайтам.")
        hint.setWordWrap(True)
        hint.setObjectName("settingsHint")
        form.addRow(hint)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        form.addRow(buttons)

        def accept():
            if not name.text().strip():
                hint.setText("Укажите название профиля.")
            elif mode.currentData() == "custom" and (not agent.text().strip() or not valid_agent(agent.text())):
                hint.setText("Укажите User-Agent из печатных символов ASCII.")
            else:
                dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.resize(480, 0)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return None
        result = {
            "id": profile.get("id") or uuid4().hex,
            "name": name.text().strip(),
            "persistent": persistent.isChecked(),
            "ua_mode": mode.currentData(),
            "ua": agent.text().strip() if mode.currentData() == "custom" else "",
        }
        dialog.deleteLater()
        return result

    def _ai_page(self):
        """AI assistant configuration (OpenAI-compatible chat completions API)."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        intro = QtWidgets.QLabel(
            'ИИ-агент CodeLane работает через API чат-моделей (OpenAI-совместимый '
            'формат). Поддерживаются DeepSeek, OpenAI, OpenRouter, Tokenator '
            '(партнёрский агрегатор: один ключ на ~80 моделей, из России без VPN), '
            'локальные серверы (LM Studio, Ollama) и любые другие совместимые '
            'провайдеры.'
        )
        intro.setWordWrap(True)
        intro.setObjectName('settingsHint')
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self._section(form, "Подключение")

        enabled = QtWidgets.QCheckBox('Включить ИИ-агента')
        enabled.setChecked(bool(self._value('ai_enabled')))
        self.controls['ai_enabled'] = enabled
        form.addRow('', enabled)

        provider = QtWidgets.QComboBox()
        # Provider list comes from the shared registry (deepseek, openai,
        # anthropic, gemini, opencode, tokenator…, openrouter, groq, ollama,
        # lmstudio, custom).
        for spec in provider_choices():
            label = spec.label
            if any(m.free for m in spec.models) and not spec.local:
                label += " · есть бесплатные"
            provider.addItem(label, spec.key)
        idx = provider.findData(str(self._value('ai_provider')))
        provider.setCurrentIndex(max(0, idx))
        self.controls['ai_provider'] = provider
        form.addRow('Провайдер', provider)

        provider_hint = QtWidgets.QLabel(str(get_provider(
            provider.currentData() or 'deepseek').hint))
        provider_hint.setWordWrap(True)
        provider_hint.setObjectName('settingsHint')
        self.controls['ai_provider_hint'] = provider_hint
        form.addRow('', provider_hint)

        base_url = QtWidgets.QLineEdit(str(self._value('ai_base_url')))
        base_url.setPlaceholderText('https://api.deepseek.com/v1')
        self.controls['ai_base_url'] = base_url
        form.addRow('Базовый URL API', base_url)

        tls_verify = QtWidgets.QCheckBox('Проверять TLS-сертификат (рекомендуется)')
        stored_verify = self._value('ai_tls_verify')
        tls_verify.setChecked(stored_verify not in (False, 'False', 'false', 0, '0'))
        tls_verify.setToolTip(
            'Снимайте только в крайнем случае: без проверки сертификата трафик можно '
            'перехватить и прочитать. Для прокси/антивируса лучше указать его корневой '
            'сертификат в поле ниже.')
        self.controls['ai_tls_verify'] = tls_verify
        form.addRow('', tls_verify)

        ca_bundle = QtWidgets.QLineEdit(str(self._value('ai_tls_ca_bundle') or ''))
        ca_bundle.setPlaceholderText('Необязательно: PEM- или DER-файл корневого сертификата')
        ca_bundle.setToolTip(
            'Корневой сертификат, которому нужно доверять дополнительно к системному '
            'хранилищу и набору Mozilla. Помогает при ошибке «unable to get local issuer '
            'certificate» и при перехвате HTTPS корпоративным прокси.')
        self.controls['ai_tls_ca_bundle'] = ca_bundle
        ca_row = QtWidgets.QWidget()
        ca_lay = QtWidgets.QHBoxLayout(ca_row)
        ca_lay.setContentsMargins(0, 0, 0, 0)
        browse_ca = QtWidgets.QPushButton('Обзор…')
        browse_ca.setToolTip('Выбрать файл сертификата CA (PEM или DER)')

        def _pick_ca_bundle():
            path, _selected = QtWidgets.QFileDialog.getOpenFileName(
                self, 'Выбрать корневой сертификат', ca_bundle.text().strip(),
                'Сертификаты (*.pem *.crt *.cer *.der);;Все файлы (*)')
            if path:
                ca_bundle.setText(path)

        browse_ca.clicked.connect(_pick_ca_bundle)
        ca_lay.addWidget(ca_bundle, 1)
        ca_lay.addWidget(browse_ca)
        form.addRow('Сертификат CA', ca_row)

        ca_hint = QtWidgets.QLabel(
            'CodeLane доверяет системному хранилищу и полному набору корневых '
            'сертификатов Mozilla (certifi). Если сеть перехватывает HTTPS, укажите '
            'корневой сертификат прокси или антивируса в этом поле.')
        ca_hint.setWordWrap(True)
        ca_hint.setObjectName('settingsHint')
        form.addRow('', ca_hint)

        def _sync_tls_fields():
            ca_bundle.setEnabled(tls_verify.isChecked())
            browse_ca.setEnabled(tls_verify.isChecked())

        tls_verify.toggled.connect(lambda _checked: _sync_tls_fields())
        _sync_tls_fields()

        key_edit = QtWidgets.QLineEdit(str(self._value('ai_api_key')))
        key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText('sk-...')
        self.controls['ai_api_key'] = key_edit
        key_row = QtWidgets.QWidget()
        key_lay = QtWidgets.QHBoxLayout(key_row)
        key_lay.setContentsMargins(0, 0, 0, 0)
        show_btn = QtWidgets.QToolButton()
        show_btn.setText('Показать')
        show_btn.setCheckable(True)

        def _toggle_secret(checked):
            key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if checked else QtWidgets.QLineEdit.EchoMode.Password)
            show_btn.setText('Скрыть' if checked else 'Показать')

        show_btn.toggled.connect(_toggle_secret)
        key_lay.addWidget(key_edit, 1)
        key_lay.addWidget(show_btn)
        # One-click way to get a *real* key: opens the provider's key page.
        get_key_btn = QtWidgets.QPushButton('Получить ключ…')
        _initial_spec = get_provider(provider.currentData() or 'deepseek')
        if _initial_spec.key_url:
            get_key_btn.setToolTip(f'Открыть {_initial_spec.key_url}')
        else:
            get_key_btn.setToolTip(
                'Ключ для этого провайдера выдаёт партнёр — вставьте его в поле ниже')
            get_key_btn.setEnabled(False)
        get_key_btn.clicked.connect(self._open_provider_keys_page)
        key_lay.addWidget(get_key_btn)
        form.addRow('API-ключ', key_row)

        temp = QtWidgets.QDoubleSpinBox()
        temp.setRange(0.0, 2.0)
        temp.setSingleStep(0.1)
        temp.setDecimals(1)
        temp.setValue(max(0.0, min(2.0, float(self._value('ai_temperature')) / 10.0)))
        self.controls['_ai_temperature_float'] = temp
        self._section(form, 'Настройка ответов')
        form.addRow('Температура', temp)

        tokens = QtWidgets.QSpinBox()
        # 0 = no local budget: the provider's own maximum applies. A hard cap
        # here is what used to cut answers off mid-sentence.
        tokens.setRange(0, 200000)
        tokens.setSingleStep(1024)
        tokens.setSuffix(' токенов')
        tokens.setSpecialValueText('без ограничения')
        tokens.setToolTip('Необязательный лимит на один ответ. 0 — без ограничения, '
                          'модель отвечает столько, сколько нужно.')
        stored_tokens = int(self._value('ai_max_tokens') or 0)
        # Values at or below the old 8192 default were a limit, not a user choice.
        tokens.setValue(0 if stored_tokens <= 8192 else stored_tokens)
        self.controls['ai_max_tokens'] = tokens
        form.addRow('Лимит токенов ответа', tokens)

        reasoning = QtWidgets.QComboBox()
        for key in ("auto", "none", "low", "medium", "high"):
            reasoning.addItem(f"{REASONING_LABELS[key]} — {REASONING_HINTS[key]}", key)
        idx = reasoning.findData(str(self._value('ai_reasoning_effort') or 'auto'))
        reasoning.setCurrentIndex(max(0, idx))
        reasoning.setToolTip('Сколько «думать» перед ответом. Для моделей, которые не '
                             'поддерживают параметр, CodeLane один раз повторит запрос без него.')
        self.controls['ai_reasoning_effort'] = reasoning
        form.addRow('Уровень размышлений', reasoning)

        prompt = QtWidgets.QLineEdit(str(self._value('ai_system_prompt')))
        prompt.setPlaceholderText('Дополнительные инструкции для агента (необязательно)')
        self.controls['ai_system_prompt'] = prompt
        form.addRow('Системная подсказка', prompt)

        # Context scope: project / open files / file / none.
        scope = QtWidgets.QComboBox()
        for key, label in (("none", "Без контекста (независимый запрос)"),
                           ("file", "Текущий файл"),
                           ("open_files", "Открытые файлы (включая несохранённые правки)"),
                           ("project", "Проект целиком")):
            scope.addItem(label, key)
        idx = scope.findData(str(self._value('ai_context_scope')))
        scope.setCurrentIndex(max(0, idx))
        self.controls['ai_context_scope'] = scope
        self._section(form, 'Контекст и поведение')
        form.addRow('Контекст запроса', scope)

        show_reasoning = QtWidgets.QCheckBox('Показывать, сколько модель размышляет')
        show_reasoning.setChecked(bool(self._value('ai_show_reasoning')))
        show_reasoning.setToolTip(
            'Счётчик размышлений виден только во время ответа и удаляется после него')
        self.controls['ai_show_reasoning'] = show_reasoning
        form.addRow('', show_reasoning)

        # Agent behaviour: full answers vs. Socratic tutoring.
        mode = QtWidgets.QComboBox()
        for key, label in (("solve", "Решение — агент даёт готовые ответы"),
                           ("teach", "Наставник — ведёт вопросами и не решает")):
            mode.addItem(label, key)
        idx = mode.findData(str(self._value('ai_mode') or 'solve'))
        mode.setCurrentIndex(max(0, idx))
        mode.setToolTip('В режиме «Наставник» агент не выдаёт готовый код, '
                        'а задаёт наводящие вопросы')
        self.controls['ai_mode'] = mode
        form.addRow('Режим агента', mode)

        strictness = QtWidgets.QComboBox()
        for key, label, hint in (
                ("soft", "Мягкий — примеры до 5 строк", TEACH_STRICTNESS_HINTS['soft']),
                ("normal", "Обычный — примеры до 3 строк", TEACH_STRICTNESS_HINTS['normal']),
                ("strict", "Строгий — код запрещён", TEACH_STRICTNESS_HINTS['strict'])):
            strictness.addItem(label, key)
        idx = strictness.findData(str(self._value('ai_teach_strictness') or 'normal'))
        strictness.setCurrentIndex(max(0, idx))
        strictness.setToolTip('Насколько строго наставник скрывает готовый код. '
                              'Действует только в режиме «Наставник».')
        self.controls['ai_teach_strictness'] = strictness
        form.addRow('Строгость наставника', strictness)

        def _sync_strictness_state():
            strictness.setEnabled(mode.currentData() == 'teach')

        mode.currentIndexChanged.connect(lambda _idx: _sync_strictness_state())
        _sync_strictness_state()

        layout.addLayout(form)

        profiles_box = QtWidgets.QGroupBox("Пользовательские профили агента")
        profiles_layout = QtWidgets.QVBoxLayout(profiles_box)
        profile_list = QtWidgets.QListWidget()
        profile_list.setMinimumHeight(96)
        profiles = self.settings_manager.get_setting("ai_profiles", [])
        self._ai_profiles = [item for item in profiles if isinstance(item, dict)] if isinstance(profiles, list) else []
        active_profile = QtWidgets.QComboBox()
        self.controls["ai_active_profile"] = active_profile

        def refresh_profiles(selected_id=None):
            if selected_id is None:
                selected_id = active_profile.currentData() or self._value("ai_active_profile") or ""
            profile_list.clear()
            active_profile.blockSignals(True)
            active_profile.clear()
            if not self._ai_profiles:
                active_profile.addItem("Добавьте профиль модели", "")
            for item in self._ai_profiles:
                name = str(item.get("name") or "Профиль")
                model_id = str(item.get("model") or "")
                active_profile.addItem(name, str(item.get("id") or ""))
                row = QtWidgets.QListWidgetItem(f"{name}  ·  {model_id}")
                row.setData(QtCore.Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
                profile_list.addItem(row)
            index = active_profile.findData(selected_id)
            active_profile.setCurrentIndex(index if index >= 0 else 0)
            active_profile.blockSignals(False)

        self._ai_profiles_list = profile_list
        profiles_layout.addWidget(profile_list)
        profiles_layout.addWidget(QtWidgets.QLabel("Профиль, который использует ИИ-агент"))
        profiles_layout.addWidget(active_profile)
        profile_buttons = QtWidgets.QHBoxLayout()
        add_profile = QtWidgets.QPushButton("Добавить")
        edit_profile = QtWidgets.QPushButton("Изменить")
        remove_profile = QtWidgets.QPushButton("Удалить")
        profile_buttons.addWidget(add_profile)
        profile_buttons.addWidget(edit_profile)
        profile_buttons.addWidget(remove_profile)
        profile_buttons.addStretch(1)
        profiles_layout.addLayout(profile_buttons)

        def selected_profile():
            item = profile_list.currentItem()
            key = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
            return next((p for p in self._ai_profiles if str(p.get("id")) == str(key)), None)

        def add_or_edit(profile=None):
            result = self._edit_ai_profile(profile)
            if result is None:
                return
            entries = [p for p in self._ai_profiles if p.get("id") != result["id"]]
            if len(entries) >= 40:
                QtWidgets.QMessageBox.information(self, "Профили ИИ", "Доступно до 40 профилей.")
                return
            self._ai_profiles = [*entries, result]
            refresh_profiles(result["id"])

        add_profile.clicked.connect(lambda: add_or_edit())
        edit_profile.clicked.connect(lambda: add_or_edit(selected_profile()) if selected_profile() else None)
        remove_profile.clicked.connect(lambda: self._remove_ai_profile(selected_profile(), refresh_profiles)
                                       if selected_profile() else None)
        profile_list.itemDoubleClicked.connect(lambda _item: add_or_edit(selected_profile()))
        refresh_profiles()
        layout.addWidget(profiles_box)

        tools_box = QtWidgets.QGroupBox('Разрешения и инструменты')
        tools_layout = QtWidgets.QVBoxLayout(tools_box)
        tools_layout.setSpacing(6)
        from services.agent_tools import ACCESS_MODES, normalize_access_mode
        access = QtWidgets.QComboBox()
        for key, label in ACCESS_MODES.items():
            access.addItem(label, key)
        access.setCurrentIndex(access.findData(normalize_access_mode(self._value('ai_access_mode'))))
        access.setToolTip('Только чтение: просмотр и поиск в проекте. Только запись: создание файлов проекта с подтверждением. '
                          'Полный доступ: файлы всего компьютера, CMD, PowerShell и приложения без подтверждений CodeLane. '
                          'Повышение прав всё ещё требует согласия Windows UAC. Применяется со следующего запроса.')
        self.controls['ai_access_mode'] = access
        tools_layout.addWidget(QtWidgets.QLabel('Разрешения агента'))
        tools_layout.addWidget(access)
        allow_edits = QtWidgets.QCheckBox('Разрешить агенту изменять файлы')
        allow_edits.setChecked(bool(self._value('ai_allow_file_edits')))
        allow_edits.setToolTip('В полном режиме доступны файлы всего компьютера без отдельных подтверждений CodeLane')
        self.controls['ai_allow_file_edits'] = allow_edits
        tools_layout.addWidget(allow_edits)
        allow_cmds = QtWidgets.QCheckBox('Разрешить команды в терминале и запуск приложений')
        allow_cmds.setChecked(bool(self._value('ai_allow_commands')))
        allow_cmds.setToolTip('В полном режиме команды CMD и PowerShell выполняются без отдельных подтверждений CodeLane')
        self.controls['ai_allow_commands'] = allow_cmds
        tools_layout.addWidget(allow_cmds)
        layout.addWidget(tools_box)

        # ---------------------------------------------------------------
        # MCP servers
        # ---------------------------------------------------------------
        mcp_box = QtWidgets.QGroupBox('MCP-серверы (Model Context Protocol)')
        mcp_layout = QtWidgets.QVBoxLayout(mcp_box)
        mcp_layout.setSpacing(6)
        mcp_enabled = QtWidgets.QCheckBox('Использовать MCP-инструменты в чате')
        mcp_enabled.setChecked(bool(self._value('ai_mcp_enabled')))
        self.controls['ai_mcp_enabled'] = mcp_enabled
        mcp_layout.addWidget(mcp_enabled)

        self.mcp_edit = QtWidgets.QPlainTextEdit()
        self.mcp_edit.setPlaceholderText(MCP_EXAMPLE)
        self.mcp_edit.setPlainText(str(self._value('ai_mcp_servers') or ''))
        self.mcp_edit.setFixedHeight(110)
        self.controls['ai_mcp_servers'] = self.mcp_edit
        mcp_layout.addWidget(self.mcp_edit)

        mcp_row = QtWidgets.QHBoxLayout()
        mcp_test = QtWidgets.QPushButton('Проверить MCP')
        self._mcp_test_button = mcp_test
        mcp_test.clicked.connect(self._test_mcp)
        mcp_example_btn = QtWidgets.QPushButton('Вставить пример')
        mcp_example_btn.clicked.connect(lambda: self.mcp_edit.setPlainText(MCP_EXAMPLE))
        mcp_row.addWidget(mcp_test)
        mcp_row.addWidget(mcp_example_btn)
        mcp_row.addStretch()
        mcp_layout.addLayout(mcp_row)
        self.mcp_status_label = QtWidgets.QLabel('')
        self.mcp_status_label.setWordWrap(True)
        self.mcp_status_label.setObjectName('settingsHint')
        mcp_layout.addWidget(self.mcp_status_label)
        layout.addWidget(mcp_box)

        test_row = QtWidgets.QHBoxLayout()
        test_btn = QtWidgets.QPushButton('Проверить API')
        test_btn.clicked.connect(self._test_ai_connection)
        clear_btn = QtWidgets.QPushButton('Очистить ключ')
        clear_btn.clicked.connect(lambda: key_edit.setText(''))
        open_agent = QtWidgets.QPushButton('Открыть агента')
        open_agent.clicked.connect(self._open_ai_agent)
        test_row.addWidget(test_btn)
        test_row.addWidget(clear_btn)
        test_row.addStretch()
        test_row.addWidget(open_agent)
        layout.addLayout(test_row)

        self.ai_status_label = QtWidgets.QLabel('')
        self.ai_status_label.setWordWrap(True)
        self.ai_status_label.setObjectName('settingsHint')
        layout.addWidget(self.ai_status_label)

        def _stored_keys() -> dict:
            """Per-account keys, normalised to ``credential_key``.

            Older settings stored a key per provider key; routes that share one
            account are folded together so the key is asked for only once.
            """
            raw = self._value('ai_provider_keys') or {}
            if not isinstance(raw, dict):
                return {}
            keys = {}
            for key, value in raw.items():
                text = str(value or '').strip()
                if not text:
                    continue
                group = (get_provider(key).credential_key
                         if str(key) in PROVIDERS else str(key))
                keys[group] = text
            return keys

        state = {'provider': provider.currentData() or 'deepseek'}
        self.controls['ai_provider_keys'] = _stored_keys()

        def _remember_current_key():
            """Store the typed key under the account it belongs to."""
            keys = self.controls['ai_provider_keys']
            value = key_edit.text().strip()
            current = state['provider']
            # Routes that share one account (Tokenator OpenAI/Claude, RF/VPN)
            # keep a single key, so it does not have to be typed again.
            group = get_provider(current).credential_key
            if value:
                keys[group] = value
            else:
                keys.pop(group, None)
            # Keep the flat setting in sync with the active provider.
            if current == (provider.currentData() or 'deepseek'):
                self.settings_manager.settings['ai_api_key'] = value

        key_edit.editingFinished.connect(_remember_current_key)

        def _provider_changed(index):
            key = provider.itemData(index)
            # Persist the key of the provider we are leaving *before* switching.
            _remember_current_key()
            state['provider'] = key
            for spec in provider_choices():
                if spec.key == key:
                    if spec.base_url:
                        base_url.setText(spec.base_url)
                    if spec.key_url:
                        get_key_btn.setToolTip(f'Открыть {spec.key_url}')
                        get_key_btn.setEnabled(True)
                    else:
                        get_key_btn.setToolTip(
                            'Ключ для этого провайдера выдаёт партнёр — вставьте его в поле ниже')
                        get_key_btn.setEnabled(False)
                    saved = self.controls['ai_provider_keys'].get(
                        spec.credential_key, '')
                    key_edit.setText(saved)
                    key_edit.setPlaceholderText(
                        'Ключ сохранён для этого провайдера' if saved
                        else f"Ключ {spec.label}")
                    break

        provider.currentIndexChanged.connect(_provider_changed)
        # Restore the key stored for the active provider's account (it may have
        # been entered through another route of the same account).
        _active_key = self.controls['ai_provider_keys'].get(
            get_provider(state['provider']).credential_key)
        if _active_key:
            key_edit.setText(_active_key)
        layout.addStretch()
        return page

    def _test_mcp(self):
        """Connect to configured MCP servers on the shared Python pool."""
        from services.mcp_client import parse_servers
        if self._mcp_test_worker is not None:
            return
        try:
            servers = parse_servers(self.mcp_edit.toPlainText())
        except Exception as exc:  # noqa: BLE001
            self.mcp_status_label.setText(f'✗ {exc}')
            return
        if not servers:
            self.mcp_status_label.setText('Список MCP-серверов пуст.')
            return
        self._mcp_test_button.setEnabled(False)
        bridge = _MCPProgressBridge(self)
        bridge.progress.connect(self.mcp_status_label.setText)
        job = run_async(
            lambda: check_mcp_servers(servers, bridge.progress.emit),
            parent=self,
            on_done=self._mcp_test_done,
            on_error=self._mcp_test_failed)
        self._mcp_test_worker = job

    def _mcp_test_done(self, lines):
        try:
            self._show_mcp_test_results(lines)
        finally:
            self._mcp_test_finished()

    def _mcp_test_failed(self, message):
        try:
            self.mcp_status_label.setText(f'✗ {message}')
        finally:
            self._mcp_test_finished()

    def _show_mcp_test_results(self, lines):
        self.mcp_status_label.setText('\n'.join(lines) or 'Нет активных MCP-серверов.')

    def _mcp_test_finished(self):
        self._mcp_test_worker = None
        self._mcp_test_button.setEnabled(True)

    def _open_provider_keys_page(self):
        """Open the active provider's API-key page."""
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        provider = self.controls.get('ai_provider')
        key = provider.currentData() if provider is not None else 'deepseek'
        url = get_provider(key).key_url
        if not url:
            label = getattr(self, 'ai_status_label', None)
            if label is not None:
                label.setText('Ключ для этого провайдера выдаёт партнёр — '
                              'вставьте полученную строку в поле «API-ключ».')
            return
        QDesktopServices.openUrl(QUrl(url))

    def _edit_ai_profile(self, profile=None):
        """Create an API profile with user-supplied endpoint, key and model ID."""
        from uuid import uuid4
        profile = dict(profile or {})
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Профиль модели")
        dialog.setMinimumWidth(480)
        form = QtWidgets.QFormLayout(dialog)
        name = QtWidgets.QLineEdit(str(profile.get("name") or ""))
        name.setPlaceholderText("Название модели в меню")
        url = QtWidgets.QLineEdit(str(profile.get("base_url") or ""))
        url.setPlaceholderText("https://api.example.com/v1")
        key = QtWidgets.QLineEdit(str(profile.get("api_key") or ""))
        key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        key.setPlaceholderText("API-ключ профиля")
        model = QtWidgets.QLineEdit(str(profile.get("model") or ""))
        model.setPlaceholderText("Точный ID модели для API")
        for label, widget in (("Название модели", name), ("URL API", url),
                              ("API-ключ", key), ("ID модели", model)):
            form.addRow(label, widget)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        while dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            if not all((name.text().strip(), url.text().strip(), model.text().strip())):
                QtWidgets.QMessageBox.information(self, "Профиль модели",
                                                  "Заполните название, URL и ID модели.")
                continue
            if not url.text().strip().startswith(("https://", "http://")):
                QtWidgets.QMessageBox.information(self, "Профиль модели",
                                                  "URL должен начинаться с https:// или http://.")
                continue
            return {
                "id": str(profile.get("id") or uuid4().hex),
                "name": name.text().strip(),
                "provider": str(profile.get("provider") or "custom"),
                "base_url": url.text().strip(),
                "api_key": key.text().strip(),
                "model": model.text().strip(),
                "protocol": str(profile.get("protocol") or (
                    "anthropic" if profile.get("provider") == "anthropic" or
                    "anthropic" in url.text().lower() else "openai")),
                "system_prompt": str(profile.get("system_prompt") or ""),
            }
        return None

    def _remove_ai_profile(self, profile, refresh):
        if profile is None:
            return
        profile_id = str(profile.get("id") or "")
        self._ai_profiles = [item for item in self._ai_profiles
                             if str(item.get("id") or "") != profile_id]
        active = self.controls.get("ai_active_profile")
        if active is not None and str(active.currentData() or "") == profile_id:
            refresh("")
        else:
            refresh()

    def _collect_ai_settings(self):
        """Read the AI controls into a plain dict (without touching the store)."""
        def text(key, default=''):
            w = self.controls.get(key)
            if isinstance(w, QtWidgets.QLineEdit):
                return w.text().strip()
            if isinstance(w, QtWidgets.QComboBox):
                return w.currentText().strip() or default
            return default

        temp_widget = self.controls.get('_ai_temperature_float')
        try:
            temperature = int(round(temp_widget.value() * 10)) if temp_widget else 3
        except Exception:
            temperature = 3
        keys = dict(self.controls.get('ai_provider_keys') or {})
        current_provider = self.controls['ai_provider'].currentData() or 'custom'
        typed_key = text('ai_api_key')
        # Providers that share one account (Tokenator routes) also share a key.
        group = get_provider(current_provider).credential_key
        if typed_key:
            keys[group] = typed_key
        saved_provider = str(self.settings_manager.get_setting('ai_provider') or '')
        model_control = self.controls.get('ai_model')
        if isinstance(model_control, QtWidgets.QComboBox):
            model_id = str(model_control.currentData() or model_control.currentText() or '')
        else:
            model_id = (str(self.settings_manager.get_setting('ai_model') or '')
                        if current_provider == saved_provider else get_provider(current_provider).default_model)
        return {
            'ai_enabled': bool(self.controls['ai_enabled'].isChecked()),
            'ai_provider': current_provider,
            'ai_base_url': text('ai_base_url'),
            'ai_model': model_id,
            'ai_api_key': typed_key,
            'ai_provider_keys': keys,
            'ai_tls_verify': bool(self.controls['ai_tls_verify'].isChecked()),
            'ai_tls_ca_bundle': text('ai_tls_ca_bundle'),
            'ai_show_reasoning': bool(self.controls['ai_show_reasoning'].isChecked()),
            'ai_mode': self.controls['ai_mode'].currentData() or 'solve',
            'ai_access_mode': self.controls['ai_access_mode'].currentData(),
            'ai_teach_strictness': (self.controls['ai_teach_strictness'].currentData()
                                    or 'normal'),
            'ai_temperature': temperature,
            'ai_max_tokens': int(self.controls['ai_max_tokens'].value()),
            'ai_profiles': list(getattr(self, '_ai_profiles', [])),
            'ai_active_profile': str(self.controls['ai_active_profile'].currentData() or ''),
            'ai_reasoning_effort': str(self.controls['ai_reasoning_effort'].currentData()
                                       or 'auto'),
            'ai_system_prompt': text('ai_system_prompt'),
        }

    def _persist_ai_settings(self) -> dict:
        values = self._collect_ai_settings()
        self.settings_manager.settings.update(values)
        return values

    def _test_ai_connection(self):
        values = self._persist_ai_settings()
        self.settings_manager.save_settings()
        from services.ai_service import AIService
        service = AIService(config=values)
        if not service.is_configured():
            self.ai_status_label.setText('Укажите API-ключ и модель, затем повторите проверку.')
            return
        self.ai_status_label.setText('Проверка подключения…')
        from services.background import run_async

        def completed(result):
            ok, message = result
            self.ai_status_label.setText(('✓ ' if ok else '✗ ') + message)

        run_async(service.test_connection, on_done=completed,
                  on_error=lambda message: self.ai_status_label.setText('✗ ' + message))

    def _open_ai_agent(self):
        self._persist_ai_settings()
        self.settings_manager.save_settings()
        window = self.main_window
        panel = getattr(window, "ai_panel", None) if window is not None else None
        if panel is not None:
            panel._refresh_menus()
            panel.refresh_status()
        if window is not None and hasattr(window, 'show_ai_agent'):
            window.show_ai_agent()

    def apply_settings(self, save_file=True):
        record_action("settings_apply_requested")
        toolchains = dict(self.settings_manager.get_setting('toolchains', {}) or {})
        theme = self.controls.get('theme')
        if theme is not None and theme.currentData() != self.settings_manager.get_setting('theme'):
            self.settings_manager.settings.update(THEMES.get(theme.currentData(), {}))
        for key, widget in self.controls.items():
            if key.startswith('_ai_temperature'):
                continue
            if key.startswith('ai_'):
                continue
            if key.startswith('toolchain_'):
                ext = key.split('_', 1)[1]
                current = dict(toolchains.get(ext, {}))
                current['command'] = widget.text().strip()
                toolchains[ext] = current
                continue
            if isinstance(widget, QtWidgets.QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QtWidgets.QSpinBox):
                value = widget.value()
            elif isinstance(widget, QtWidgets.QSlider):
                value = int(widget.value())
            elif isinstance(widget, QtWidgets.QComboBox):
                value = widget.currentData() if widget.currentData() is not None else widget.currentText()
            else:
                value = widget.text()
            self.settings_manager.settings[key] = value
        if 'ai_enabled' in self.controls:
            self._persist_ai_settings()
        self.settings_manager.settings['toolchains'] = toolchains

        sm = self.settings_manager
        sm.settings['main_color'] = sm.get_setting('accent_color', DEFAULTS['accent_color'])
        sm.settings['text_color'] = sm.get_setting('text_primary', DEFAULTS['text_primary'])
        sm.settings['first_color'] = sm.get_setting('app_background', DEFAULTS['app_background'])
        sm.settings['second_color'] = sm.get_setting('editor_background', DEFAULTS['editor_background'])
        sm.settings['tab_color'] = sm.get_setting('accent_color', DEFAULTS['accent_color'])
        sm.settings['fontsize'] = str(sm.get_setting('editor_font_size', 13))
        sm.settings['font_size_tab'] = str(sm.get_setting('ui_font_size', 13))
        legacy = dict(sm.get_setting('settings', {}))
        legacy.update({'main_color': sm.get_setting('accent_color', DEFAULTS['accent_color']),
                       'text_color': sm.get_setting('text_primary', DEFAULTS['text_primary']),
                       'first_color': sm.get_setting('app_background', DEFAULTS['app_background']),
                       'second_color': sm.get_setting('editor_background', DEFAULTS['editor_background']),
                       'tab_color': sm.get_setting('accent_color', DEFAULTS['accent_color']),
                       'fontsize': str(sm.get_setting('editor_font_size', 13)),
                       'font_size_tab': str(sm.get_setting('ui_font_size', 13))})
        sm.settings['settings'] = legacy
        if save_file and not sm.save_settings():
            self._apply_status.setText("Не удалось сохранить настройки")
            return
        self._apply_status.setText("Настройки применены" if not save_file else "Настройки сохранены")

        if self.main_window:
            if hasattr(self.main_window, 'app_manager'):
                self.main_window.app_manager.settings = sm.get_all_settings()
            panel = getattr(self.main_window, 'ai_panel', None)
            if panel is not None:
                panel._refresh_menus()
                panel.refresh_status()
        if self.main_window and hasattr(self.main_window, 'app_manager'):
            if hasattr(self.main_window, 'auto_save_timer') and self.main_window.auto_save_timer:
                interval = int(sm.get_setting('auto_save_interval', 30)) * 1000
                self.main_window.auto_save_timer.start(interval) if interval else self.main_window.auto_save_timer.stop()
            if hasattr(self.main_window, 'apply_application_theme'):
                self.main_window.apply_application_theme()
            elif hasattr(self.main_window, 'apply_interface_settings'):
                self.main_window.apply_interface_settings()
            self.main_window.status_bar.showMessage('Настройки применены')

    def save_settings(self):
        self.apply_settings()
