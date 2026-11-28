"""Project analysis selection expressed as goals and concrete checks."""
from PyQt6 import QtCore, QtGui, QtWidgets


class AnalyzerProfilesEditor(QtWidgets.QWidget):
    apply_requested = QtCore.pyqtSignal(list)
    open_analysis_requested = QtCore.pyqtSignal()
    PROFILES = {
        "recommended": ("Общий обзор", "Познакомиться с проектом и найти проблемные места.", {"structure", "dependencies", "health"}),
        "quality": ("Улучшить код", "Найти сложные функции и накопившиеся недоработки.", {"structure", "metrics", "health"}),
        "security": ("Проверить безопасность", "Найти потенциально опасные конструкции в коде.", {"structure", "security"}),
        "architecture": ("Разобраться в связях", "Узнать, какие части проекта зависят друг от друга.", {"structure", "dependencies"}),
        "deep": ("Все проверки", "Проверить код, его связи и скомпилированные файлы.", {"structure", "dependencies", "metrics", "health", "security", "binaries"}),
        "none": ("Без фонового анализа", "Отключить фоновые проверки проекта.", set()),
    }
    ANALYZER_INFO = {
        "structure": ("Карта кода", "Найдёт файлы, классы и функции. Поможет ориентироваться в проекте."),
        "dependencies": ("Связи между файлами", "Покажет импорты и зависимости: какие части кода связаны друг с другом."),
        "metrics": ("Размер и сложность кода", "Оценит объём кода и сложность функций, чтобы выделить трудные для поддержки места."),
        "health": ("Недоработки и качество", "Найдёт признаки проблем в коде и пометки TODO / FIXME. Сформирует общую оценку качества."),
        "security": ("Риски безопасности", "Проверит потенциально опасные конструкции статическим анализом. Это не гарантия отсутствия уязвимостей."),
        "binaries": ("Скомпилированные файлы", "Изучит структуру и метаданные EXE, DLL, ELF и других бинарных файлов, если они есть в проекте."),
    }
    CHECK_GROUPS = (
        ("Структура", ("structure", "dependencies")),
        ("Качество", ("metrics", "health")),
        ("Риски и артефакты", ("security", "binaries")),
    )

    def __init__(self, project, current=None, parent=None):
        super().__init__(parent)
        self.project = project
        self.selected_analyzers = set(self.PROFILES["recommended"][2] if current is None else current)
        self._syncing = False
        self._checks = {}
        self._group_dividers = []
        self._wide = None
        self.setObjectName("analysisChooser")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("pyscribeSpecial", "projectAnalyzers")
        name = project.get("name", "Проект") if isinstance(project, dict) else project.name
        self.setFont(QtGui.QFont("Segoe UI", 9))
        self._build(name)
        self._update_plan()

    def _label(self, text, role, parent_layout):
        label = QtWidgets.QLabel(text)
        label.setObjectName(role)
        label.setWordWrap(True)
        label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        parent_layout.addWidget(label)
        return label

    def _build(self, name):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("analysisChooserScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)
        self._viewport = scroll.viewport()
        self._viewport.installEventFilter(self)

        page = QtWidgets.QWidget()
        page.setObjectName("analysisProfilePage")
        scroll.setWidget(page)
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(20, 22, 20, 22)
        page_layout.setSpacing(0)
        content = QtWidgets.QWidget()
        content.setObjectName("analysisProfileContent")
        content.setMaximumWidth(1040)
        centered = QtWidgets.QHBoxLayout()
        centered.setContentsMargins(0, 0, 0, 0)
        centered.addStretch()
        centered.addWidget(content, 40)
        centered.addStretch()
        page_layout.addLayout(centered)
        page_layout.addStretch()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        self._label("Анализаторы проекта", "analysisChooserTitle", content_layout)
        self._label(name, "analysisChooserHint", content_layout)
        content_layout.addSpacing(10)
        panel = QtWidgets.QFrame()
        panel.setObjectName("analysisChecksPanel")
        content_layout.addWidget(panel)
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 14, 16, 14)
        panel_layout.setSpacing(14)

        self.profile_row = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
        profile_label = self._label("Профиль", "analysisChecksTitle", self.profile_row)
        self.profile_picker = QtWidgets.QComboBox()
        self.profile_picker.setObjectName("analysisProfilePicker")
        profile_label.setBuddy(self.profile_picker)
        self.profile_picker.setAccessibleName("Профиль проверок")
        self.profile_picker.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.profile_picker.setMinimumContentsLength(14)
        for key, (title, description, _) in self.PROFILES.items():
            self.profile_picker.addItem(title, key)
            self.profile_picker.setItemData(self.profile_picker.count() - 1, description,
                                            QtCore.Qt.ItemDataRole.ToolTipRole)
        self.profile_picker.addItem("Свой набор", "custom")
        self.profile_picker.currentIndexChanged.connect(self._profile_selected)
        self.profile_row.addWidget(self.profile_picker, 1)
        self.plan_text = self._label("", "analysisChooserHint", self.profile_row)
        panel_layout.addLayout(self.profile_row)
        panel_layout.addWidget(self._divider())

        self.groups_layout = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
        self.groups_layout.setSpacing(14)
        for heading, keys in self.CHECK_GROUPS:
            if self.groups_layout.count():
                divider = self._divider()
                self._group_dividers.append(divider)
                self.groups_layout.addWidget(divider)
            branch = QtWidgets.QWidget()
            branch.setObjectName("analysisProfileGroup")
            branch_layout = QtWidgets.QVBoxLayout(branch)
            branch_layout.setContentsMargins(0, 0, 0, 0)
            branch_layout.setSpacing(9)
            self._label(heading, "analysisChooserSection", branch_layout)
            for key in keys:
                title, description = self.ANALYZER_INFO[key]
                check = QtWidgets.QCheckBox(title)
                check.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                check.setToolTip(description)
                check.setAccessibleDescription(description)
                self._checks[key] = check
                check.toggled.connect(lambda checked, k=key: self._check_changed(k, checked))
                branch_layout.addWidget(check)
            branch_layout.addStretch()
            self.groups_layout.addWidget(branch, 1)
        panel_layout.addLayout(self.groups_layout)
        panel_layout.addWidget(self._divider())

        self.result_hint = self._label("", "analysisChooserHint", panel_layout)
        actions = QtWidgets.QHBoxLayout()
        self.view_button = QtWidgets.QPushButton("К результатам")
        self.view_button.setToolTip("Открыть вкладку «Анализ проекта»")
        self.view_button.clicked.connect(self.open_analysis_requested)
        apply = QtWidgets.QPushButton("Применить")
        apply.setObjectName("primary")
        apply.clicked.connect(lambda: self.apply_requested.emit(sorted(self.selected_analyzers)))
        actions.addWidget(self.view_button)
        actions.addStretch()
        actions.addWidget(apply)
        panel_layout.addLayout(actions)
        self._reflow()

    @staticmethod
    def _divider():
        divider = QtWidgets.QFrame()
        divider.setObjectName("analysisGroupDivider")
        divider.setFixedHeight(1)
        return divider

    def eventFilter(self, watched, event):
        if watched is self._viewport and event.type() == QtCore.QEvent.Type.Resize:
            self._reflow()
        return super().eventFilter(watched, event)

    def _reflow(self):
        if not hasattr(self, "groups_layout"):
            return
        width = self._viewport.width()
        horizontal = QtWidgets.QBoxLayout.Direction.LeftToRight
        vertical = QtWidgets.QBoxLayout.Direction.TopToBottom
        self.profile_row.setDirection(horizontal if width >= 500 else vertical)
        wide = width >= 790
        if wide == self._wide:
            return
        self._wide = wide
        self.groups_layout.setDirection(horizontal if wide else vertical)
        for divider in self._group_dividers:
            maximum = QtWidgets.QWIDGETSIZE_MAX
            divider.setMinimumSize(1, 1)
            divider.setMaximumSize(1 if wide else maximum, maximum if wide else 1)

    def _profile_selected(self, index):
        key = self.profile_picker.itemData(index)
        if not self._syncing and key in self.PROFILES:
            self.selected_analyzers = set(self.PROFILES[key][2])
            self._update_plan()

    def _check_changed(self, key, checked):
        if self._syncing:
            return
        if checked:
            self.selected_analyzers.add(key)
        else:
            self.selected_analyzers.discard(key)
        self._update_plan()

    def _update_plan(self):
        self._syncing = True
        try:
            matching = next((key for key, (_, _, values) in self.PROFILES.items()
                             if values == self.selected_analyzers), None)
            self.profile_picker.setCurrentIndex(self.profile_picker.findData(matching or "custom"))
            for key, check in self._checks.items():
                check.setChecked(key in self.selected_analyzers)
            self.plan_text.setText(f"Включено {len(self.selected_analyzers)} из {len(self.ANALYZER_INFO)}")
            if self.selected_analyzers:
                self.result_hint.setText("Результаты проверок появятся во вкладке «Анализ проекта».")
            else:
                self.result_hint.setText("Фоновый анализ будет отключён.")
        finally:
            self._syncing = False


class ProjectAnalyzerDialog(QtWidgets.QDialog):
    """Compatibility wrapper for callers that need a modal profile chooser."""

    def __init__(self, project, current=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Анализаторы проекта")
        self.resize(900, 700)
        self.editor = AnalyzerProfilesEditor(project, current=current, parent=self)
        self.editor.view_button.hide()
        self.selected_analyzers = sorted(self.editor.selected_analyzers)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.editor)
        self.editor.apply_requested.connect(self._accept_selection)

    def _accept_selection(self, selected):
        self.selected_analyzers = selected
        self.accept()
