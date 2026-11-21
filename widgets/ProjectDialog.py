"""PyScribe widgets/ProjectDialog.py: application module and reusable component."""
from core.logging_config import get_logger
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QFileDialog, QTextEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QTabWidget,
                             QWidget, QFormLayout, QComboBox, QFrame, QScrollArea, QMenu, QSizePolicy)
from PyQt6.QtCore import QEvent, Qt, QSize
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPalette
from pathlib import Path
import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict, replace
from typing import Dict, List, Optional
import datetime
from widgets.ProjectManager import ProjectConfig, project_manager

class _ProjectLabel(QLabel):
    def __init__(self, text, name):
        super().__init__()
        self._full_text = text
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._update_text()

    def _update_text(self):
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()
        ))


class ModernProjectCard(QFrame):
    """Современная карточка проекта"""
    
    def __init__(self, project: ProjectConfig, parent=None):
        super().__init__(parent)
        self.project = project
        self.parent_dialog = parent
        self.is_selected = False
        self.setup_ui()
    
    def setup_ui(self):
        self.setObjectName("projectRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)
        info = QVBoxLayout()
        info.setSpacing(2)
        name = _ProjectLabel(self.project.get_display_name(), "projectRowName")
        path = _ProjectLabel(self.project.root_path, "projectRowPath")
        info.addWidget(name)
        info.addWidget(path)
        layout.addLayout(info, 1)
        self.open_button = QPushButton("Открыть")
        self.open_button.clicked.connect(self.on_open_clicked)
        self.configure_button = QPushButton("Настроить")
        self.configure_button.clicked.connect(self.on_configure_clicked)
        layout.addWidget(self.open_button)
        layout.addWidget(self.configure_button)

    def set_selected(self, selected: bool):
        self.is_selected = selected
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.parent_dialog and hasattr(self.parent_dialog, 'select_card'):
                self.parent_dialog.select_card(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.parent_dialog:
            self.parent_dialog.open_project(self.project)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if self.parent_dialog and hasattr(self.parent_dialog, 'select_card'):
            self.parent_dialog.select_card(self)
        menu = QMenu(self)
        open_action = menu.addAction("Открыть проект")
        config_action = menu.addAction("Настроить...")
        menu.addSeparator()
        delete_action = menu.addAction("Удалить из списка")
        chosen = menu.exec(event.globalPos())
        if chosen == open_action:
            self.on_open_clicked()
        elif chosen == config_action:
            self.on_configure_clicked()
        elif chosen == delete_action:
            self.on_delete_clicked()

    def on_open_clicked(self):
        """Обработчик клика на кнопку Open"""
        if self.parent_dialog:
            self.parent_dialog.open_project(self.project)
        
    def on_delete_clicked(self):
        """Обработчик клика на кнопку Delete"""
        reply = QMessageBox.question(
            self.parent_dialog or self,
            "Удалить проект",
            f"Вы уверены, что хотите удалить проект «{self.project.name}» из списка?\n\n"
            "Примечание: проект будет удален только из списка PyScribe, файлы на диске останутся нетронутыми.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            from widgets.ProjectManager import project_manager
            if project_manager.delete_project(self.project.root_path):
                if self.parent_dialog:
                    if getattr(self.parent_dialog, 'selected_card', None) == self:
                        self.parent_dialog.selected_card = None
                        self.parent_dialog.selected_project = None
                    self.parent_dialog.load_all_projects()
                else:
                    self.deleteLater()
            else:
                QMessageBox.warning(
                    self.parent_dialog or self,
                    "Не удалось удалить",
                    f"Не удалось удалить проект «{self.project.name}»."
                )
    
    def on_configure_clicked(self):
        """Обработчик клика на кнопку Configure"""
        if self.parent_dialog:
            self.parent_dialog.configure_project(self.project)

class ProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._owner = parent
        self.selected_project = None
        self.selected_card = None
        self.setWindowTitle("Открыть проект")
        self.setObjectName("projectManager")
        self.setMinimumSize(590, 340)
        self.resize(720, 460)
        self.setup_ui()

    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по названию или пути")
        self.search_edit.textChanged.connect(self.filter_projects)
        header.addWidget(self.search_edit, 1)
        folder = QPushButton("Открыть папку…")
        folder.clicked.connect(self.quick_open_folder)
        header.addWidget(folder)
        create = QPushButton("Новый проект")
        create.clicked.connect(self.show_new_project_dialog)
        header.addWidget(create)
        layout.addLayout(header)

        self.project_area = QScrollArea()
        self.project_area.setFrameShape(QFrame.Shape.NoFrame)
        self.project_area.setWidgetResizable(True)
        content = QWidget()
        self.all_layout = QVBoxLayout(content)
        self.all_layout.setContentsMargins(0, 0, 0, 0)
        self.all_layout.setSpacing(5)
        self.all_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.project_area.setWidget(content)
        layout.addWidget(self.project_area, 1)
        self.cards = []
        self.empty_projects = QLabel()
        self.empty_projects.setObjectName("projectRowPath")
        self.empty_projects.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.all_layout.addWidget(self.empty_projects)
        self.load_all_projects()
        self.search_edit.installEventFilter(self)
        self.search_edit.setFocus()

    def select_card(self, card):
        """Выбор карточки проекта"""
        if hasattr(self, 'selected_card') and self.selected_card and self.selected_card != card:
            try:
                self.selected_card.set_selected(False)
            except Exception:
                pass
        self.selected_card = card
        self.selected_project = card.project if card is not None else None
        if card:
            card.set_selected(True)

    def keyPressEvent(self, event):
        """Удаление выбранного проекта по клавише Delete/Backspace"""
        if event.key() == Qt.Key.Key_Delete and self.selected_card and not self.search_edit.hasFocus():
            self.selected_card.on_delete_clicked()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.search_edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                visible = [card for card in self.cards if not card.isHidden()]
                if visible:
                    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                        card = self.selected_card if self.selected_card in visible else visible[0]
                        self.open_project(card.project)
                    else:
                        if self.selected_card in visible:
                            step = 1 if key == Qt.Key.Key_Down else -1
                            index = max(0, min(visible.index(self.selected_card) + step, len(visible) - 1))
                        else:
                            index = 0 if key == Qt.Key.Key_Down else -1
                        card = visible[index]
                        self.select_card(card)
                        self.project_area.ensureWidgetVisible(card, 0, 8)
                return True
        return super().eventFilter(watched, event)

    def load_all_projects(self):
        """Загрузка всех проектов"""
        self.select_card(None)
        for card in self.cards:
            self.all_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards.clear()

        for project in project_manager.get_sorted_projects():
            card = ModernProjectCard(project, self)
            self.all_layout.insertWidget(len(self.cards), card)
            self.cards.append(card)

        self.empty_projects.setText("Нет совпадений" if self.cards else "Нет сохранённых проектов")
        self.filter_projects(self.search_edit.text())
    
    def filter_projects(self, text):
        """Фильтрация проектов по поисковому запросу"""
        query = text.strip().casefold()
        has_matches = False
        for card in self.cards:
            project_name = f"{card.project.get_display_name()} {card.project.root_path}".casefold()
            matches = query in project_name
            card.setVisible(matches)
            has_matches = has_matches or matches
            if not matches and card is self.selected_card:
                self.select_card(None)
        self.empty_projects.setVisible(not has_matches)
    
    def show_new_project_dialog(self):
        """Показать диалог создания нового проекта"""
        dialog = NewProjectDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if getattr(dialog, 'created_project', None):
                self.selected_project = dialog.created_project
                self.accept()
                return
            self.load_all_projects()
    
    def quick_open_folder(self):
        """Быстрое открытие папки"""
        picker = QFileDialog(self, "Открыть папку")
        picker.setOption(QFileDialog.Option.DontUseNativeDialog)
        picker.setOption(QFileDialog.Option.ShowDirsOnly)
        picker.setDirectory(str(Path(self.selected_project.root_path) if self.selected_project else Path.home()))
        picker.setFileMode(QFileDialog.FileMode.Directory)
        picker.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        picker.setLabelText(QFileDialog.DialogLabel.Accept, "Открыть")
        picker.resize(740, 480)
        folder = picker.selectedFiles()[0] if picker.exec() else ""

        if folder:
            folder = str(Path(folder).resolve())
            project = project_manager.open_project(folder)
            if not project:
                project = project_manager.create_project(folder)
            
            self.selected_project = project
            self.accept()
    
    def open_project(self, project):
        """Select a project and close the dialog.

        Project activation is deliberately handled by the owner after
        exec_() returns.  Calling the main window from inside a modal dialog
        can re-enter the event loop while the dialog is still unwinding and
        was the source of the RecursionОшибка seen when opening projects.
        """
        if project is None:
            return

        opened_project = project_manager.open_project(project.root_path)
        if opened_project is None:
            return

        self.selected_project = opened_project
        self.accept()
    
    def configure_project(self, project):
        """Настройка проекта"""
        dialog = ProjectConfigDialog(project, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            from widgets.ProjectManager import project_manager
            try:
                project_manager.update_project_config(dialog.get_config())
            except OSError as exc:
                QMessageBox.warning(self, "Не удалось сохранить проект", str(exc))
                return
            self.load_all_projects()

class NewProjectDialog(QDialog):
    """Диалог создания нового проекта"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Создание нового проекта")
        self.setMinimumSize(500, 400)
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(20)
        
        # Заголовок
        title_label = QLabel("Создание нового проекта")
        title_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(title_label)
        
        # Форма
        form_layout = QFormLayout()
        form_layout.setSpacing(15)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        
        # Название проекта
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Мой проект")
        form_layout.addRow("Название проекта:", self.name_edit)
        
        # Папка проекта
        folder_layout = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Выберите папку проекта…")
        browse_btn = QPushButton("Обзор…")
        browse_btn.clicked.connect(self.browse_folder)
        
        folder_layout.addWidget(self.folder_edit, 1)
        folder_layout.addWidget(browse_btn)
        form_layout.addRow("Папка проекта:", folder_layout)
        
        # Команда запуска
        self.command_combo = QComboBox()
        self.command_combo.addItems([
            "Определить автоматически",
            "python main.py",
            "npm start",
            "node index.js", 
            "cargo run",
            "go run main.go",
            "Своя команда…"
        ])
        self.command_combo.currentTextChanged.connect(self.on_command_changed)
        form_layout.addRow("Команда запуска:", self.command_combo)
        
        # Кастомная команда (скрыта по умолчанию)
        self.custom_command_edit = QLineEdit()
        self.custom_command_edit.setPlaceholderText("Введите команду запуска…")
        self.custom_command_edit.setVisible(False)
        form_layout.addRow("Своя команда:", self.custom_command_edit)
        
        # Описание
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(80)
        self.description_edit.setPlaceholderText("Описание проекта (необязательно)…")
        form_layout.addRow("Описание:", self.description_edit)
        
        layout.addLayout(form_layout)
        layout.addStretch()
        
        # Кнопки
        button_layout = QHBoxLayout()
        
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("secondary")
        cancel_btn.clicked.connect(self.reject)
        
        self.create_btn = QPushButton("Создать проект")
        self.create_btn.setObjectName("create-btn")
        self.create_btn.clicked.connect(self.create_project)
        self.create_btn.setEnabled(False)
        
        button_layout.addStretch()
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(self.create_btn)
        
        layout.addLayout(button_layout)
        
        # Подключаем проверку валидности
        self.name_edit.textChanged.connect(self.validate_form)
        self.folder_edit.textChanged.connect(self.validate_form)
    
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку проекта")
        if folder:
            self.folder_edit.setText(folder)
            # Автозаполнение имени проекта если не заполнено
            if not self.name_edit.text():
                self.name_edit.setText(Path(folder).name)
    
    def on_command_changed(self, text):
        """Обработчик изменения команды запуска"""
        self.custom_command_edit.setVisible(text == "Своя команда…")
    
    def validate_form(self):
        """Проверка валидности формы"""
        has_name = bool(self.name_edit.text().strip())
        has_folder = bool(self.folder_edit.text().strip()) and Path(self.folder_edit.text()).exists()
        self.create_btn.setEnabled(has_name and has_folder)
    
    def create_project(self):
        """Создание проекта"""
        name = self.name_edit.text().strip()
        folder = self.folder_edit.text().strip()
        description = self.description_edit.toPlainText().strip()
        
        # Определяем команду запуска
        command_text = self.command_combo.currentText()
        if command_text == "Определить автоматически":
            launch_command = self.auto_detect_command(folder)
        elif command_text == "Своя команда…":
            launch_command = self.custom_command_edit.text().strip()
        else:
            launch_command = command_text
        
        # Создаем проект
        project = project_manager.create_project(
            root_path=folder,
            name=name,
            launch_command=launch_command,
            description=description
        )
        
        # Создаем базовую структуру если папка пустая
        if not any(Path(folder).iterdir()):
            self.create_project_structure(folder)
        
        self.created_project = project
        self.accept()
    
    def auto_detect_command(self, folder_path):
        """Автоматическое определение команды запуска"""
        path = Path(folder_path)
        
        if (path / "package.json").exists():
            return "npm start"
        elif (path / "main.py").exists():
            return "python main.py"
        elif (path / "index.js").exists():
            return "node index.js"
        elif (path / "Cargo.toml").exists():
            return "cargo run"
        elif (path / "go.mod").exists():
            return "go run main.go"
        elif (path / "pom.xml").exists():
            return "mvn exec:java"
        elif (path / "build.gradle").exists():
            return "./gradlew run" if (path / "gradlew").exists() else "gradle run"
        
        return ""
    
    def create_project_structure(self, folder_path):
        """Создание базовой структуры проекта"""
        try:
            path = Path(folder_path)
            
            # Создаем README
            readme_content = f"""# {self.name_edit.text()}

## Описание
{self.description_edit.toPlainText() or 'Add your project description here.'}

## Getting Started
Follow these instructions to get the project up and running.

## Installation
Add installation instructions here.

## Usage
Explain how to use your project.
"""
            (path / "README.md").write_text(readme_content, encoding='utf-8')
            
            # Создаем gitignore если нет
            if not (path / ".gitignore").exists():
                gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
*.egg-info/
.installed.cfg
*.egg

# Node.js
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
"""
                (path / ".gitignore").write_text(gitignore_content, encoding='utf-8')
                
        except Exception as e:
            logger.exception("Ошибка creating project structure: ")


class ProjectConfigDialog(QDialog):
    def __init__(self, project_config, parent=None):
        super().__init__(parent)
        self.project_config = project_config
        self.setWindowTitle(f"Настроить проект: {project_config.name}")
        self.setMinimumSize(500, 400)
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(20)
        
        # Заголовок
        title_label = QLabel("Project Configuration")
        title_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(title_label)
        
        # Информация о проекте
        info_frame = QFrame()
        info_frame.setStyleSheet("""
            QFrame {
                background-color: #3E3E42;
                border-radius: 6px;
                padding: 15px;
            }
        """)
        info_layout = QVBoxLayout(info_frame)
        
        info_title = QLabel("Project Information")
        info_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #FFFFFF; margin-bottom: 10px;")
        info_layout.addWidget(info_title)
        
        # Путь проекта
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("Расположение:"))
        path_label = QLabel(self.project_config.root_path)
        path_label.setStyleSheet("color: #CCCCCC; font-family: 'Consolas', monospace;")
        path_label.setWordWrap(True)
        path_layout.addWidget(path_label, 1)
        info_layout.addLayout(path_layout)
        
        # Дата создания
        created_layout = QHBoxLayout()
        created_layout.addWidget(QLabel("Создан:"))
        try:
            created_date = datetime.datetime.fromisoformat(
                self.project_config.created_at.replace('Z', '+00:00')
            ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_date = "Неизвестно"
        created_label = QLabel(created_date)
        created_label.setStyleSheet("color: #CCCCCC;")
        created_layout.addWidget(created_label)
        info_layout.addLayout(created_layout)
        
        layout.addWidget(info_frame)
        
        # Форма настроек
        form_layout = QFormLayout()
        form_layout.setSpacing(15)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        
        # Название проекта
        self.name_edit = QLineEdit(self.project_config.name)
        self.name_edit.setPlaceholderText("Введите название проекта…")
        form_layout.addRow("Название проекта:", self.name_edit)
        
        # Команда запуска
        command_layout = QVBoxLayout()
        
        self.command_combo = QComboBox()
        self.command_combo.addItems([
            "Своя команда…",
            "python main.py",
            "npm start", 
            "node index.js",
            "java Main",
            "cargo run",
            "go run main.go",
            "./gradlew run",
            "mvn exec:java"
        ])
        
        self.custom_command_edit = QLineEdit()
        self.custom_command_edit.setPlaceholderText("Введите команду запуска…")
        self.custom_command_edit.setText(self.project_config.launch_command)
        
        # Устанавливаем правильный выбор в комбобоксе
        if self.project_config.launch_command in [self.command_combo.itemText(i) for i in range(1, self.command_combo.count())]:
            self.command_combo.setCurrentText(self.project_config.launch_command)
            self.custom_command_edit.setVisible(False)
        else:
            self.command_combo.setCurrentText("Своя команда…")
            self.custom_command_edit.setVisible(True)
        
        self.command_combo.currentTextChanged.connect(self.on_command_changed)
        
        command_layout.addWidget(self.command_combo)
        command_layout.addWidget(self.custom_command_edit)
        form_layout.addRow("Команда запуска:", command_layout)
        
        # Описание проекта
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(100)
        self.description_edit.setPlainText(self.project_config.description)
        self.description_edit.setPlaceholderText("Описание проекта...")
        form_layout.addRow("Описание:", self.description_edit)
        
        layout.addLayout(form_layout)
        layout.addStretch()
        
        # Кнопки
        button_layout = QHBoxLayout()
        
        # Кнопка тестирования команды
        test_btn = QPushButton("Проверить команду")
        test_btn.setObjectName("secondary")
        test_btn.clicked.connect(self.test_command)
        
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("secondary")
        cancel_btn.clicked.connect(self.reject)
        
        save_btn = QPushButton("Сохранить изменения")
        save_btn.setObjectName("save-btn")
        save_btn.clicked.connect(self.save_config)
        
        button_layout.addWidget(test_btn)
        button_layout.addStretch()
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(save_btn)
        
        layout.addLayout(button_layout)
    
    def on_command_changed(self, text):
        """Обработчик изменения команды запуска"""
        is_custom = text == "Своя команда…"
        self.custom_command_edit.setVisible(is_custom)
        
        if not is_custom and text != "Своя команда…":
            self.custom_command_edit.setText(text)
    
    def test_command(self):
        """Тестирование команды запуска"""
        command = self.get_launch_command()
        if not command:
            QMessageBox.information(self, "Проверить команду", "Команда запуска не указана")
            return
        
        try:
            import platform
            from core.subprocess_utils import run_hidden

            # Проверяем существование команды
            if platform.system() == "Windows":
                result = run_hidden(["where", command.split()[0]], capture_output=True, text=True)
            else:
                result = run_hidden(["which", command.split()[0]], capture_output=True, text=True)
            
            if result.returncode == 0:
                QMessageBox.information(self, "Проверить команду", 
                                      f"✅ Команда найдена:\n{command}\n\nГотово к запуску!")
            else:
                QMessageBox.warning(self, "Проверить команду",
                                  f"⚠️ Команда не найдена:\n{command}\n\n"
                                  f"Убедитесь, что команда доступна в системной переменной PATH.")
                
        except Exception as e:
            QMessageBox.critical(self, "Проверить команду Ошибка", 
                               f"Ошибка при проверке команды:\n{str(e)}")
    
    def get_launch_command(self):
        """Получение команды запуска из формы"""
        if self.command_combo.currentText() == "Своя команда…":
            return self.custom_command_edit.text().strip()
        else:
            return self.command_combo.currentText()
    
    def get_config(self):
        return replace(self.project_config, name=self.name_edit.text().strip(),
                       launch_command=self.get_launch_command(),
                       description=self.description_edit.toPlainText().strip())
    
    def save_config(self):
        """Сохранение конфигурации"""
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Ошибка проверки", "Название проекта не может быть пустым")
            return
        
        self.accept()


logger = get_logger("ProjectDialog")
