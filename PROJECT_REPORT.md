# Архитектурный паспорт и технический отчёт проекта: CodeLane

> **Сформировано автоматически в CodeLane IDE для технической документации, Code Review и системного анализа.**
> Индекс здоровья кодовой базы: **Удовлетворительное (Класс C · 3/5) · 64/100 баллов**

---

## 1. Паспорт проекта и ключевые метрики

| Параметр | Значение | Описание и архитектурное значение |
| :--- | :--- | :--- |
| **Название проекта** | `CodeLane` | Основное наименование анализируемой кодовой базы |
| **Главная точка входа** | `main.py` | Исполняемый модуль инициализации и запуска приложения |
| **Объём кодовой базы** | `64665` строк кода | Общий объём логики и структурных компонентов |
| **Количество модулей** | `188` файлов | Модульная структура с декомпозицией ответственности |
| **Классов и моделей** | `329` | Объектно-ориентированные модели данных, виджеты и сервисы |
| **Функций и методов** | `3536` | Процедурные алгоритмы и методы бизнес-логики |
| **Связей зависимостей** | `524` импортов | Карта межмодульных импортов и внешних зависимостей |
| **Диагностика техдолга** | `1790` замечаний | Обнаруженные статические проблемы (2 ошибок, 34 предупреждений) |

---

## 2. Архитектура проекта и структура компонентов

### Архитектурные слои системы:
1. **Точка входа (`main.py`)**: Инициализирует системное окружение, загружает конфигурацию и запускает основной цикл работы приложения.
2. **Пользовательский интерфейс (`widgets/`, `ui/`)**: Главное окно рабочей среды, расширенный редактор кода с подсветкой синтаксиса, терминал, боковая панель и дашборд аналитики.
3. **Ядро статического анализа (`core/`)**: AST-парсеры, построитель ориентированного графа зависимостей, расчёт цикломатической сложности и индекс символов.
4. **Служебные сервисы (`services/`)**: Аудит качества кода, генерация архитектурного паспорта, рекомендации по рефакторингу и разбор кода.
5. **Конфигурация и окружение (`config/`, `utils/`)**: Управление сессиями, пользовательскими параметрами и сторонними языковыми провайдерами.

### Ключевые модули кодовой базы:
- **`AIAgentPanel.py`** (4256 строк) — Классы: _ModelsWorker, _RequestStopped, AIWorker, _QuickAIResult; Методы и функции: _detach_worker, run, _dispatch_tool, stop, _enforce_teaching
- **`main_window.py`** (3615 строк) — Классы: ModernMainWindow; Методы и функции: _setup_tray_icon, _initialize_attributes, _setup_terminal, _terminal_visibility_changed, toggle_terminal
- **`QCodeEditor.py`** (2656 строк) — Классы: SelectionMode, EnhancedTextEdit, LineNumberArea, ModernSyntaxHighlighter; Методы и функции: _ui_font, _ui_font_family, update_rectangle_selection, finalize_rectangle_selection, get_rectangle_selection_text
- **`TerminalWidget.py`** (2577 строк) — Классы: CompletionPopup, InteractiveTerminalEdit, TerminalTab, TerminalWidget; Методы и функции: _clean_saved_terminal_output, strip_ansi, _ensure_opacity, show_items, hide_popup
- **`ProjectIntelligenceDashboard.py`** (2117 строк) — Классы: Palette, AnalysisWorker, AdversaryWorker, GraphNodeItem; Методы и функции: _ui_font, _ui_font_family, severity, primary_button_qss, secondary_button_qss
- **`ai_service.py`** (1370 строк) — Классы: AIError, AIMessage, ToolCall, AIService; Методы и функции: _is_uuid_like, as_dict, _setting, provider_key, provider
- **`SettingsWidget.py`** (1309 строк) — Классы: _MCPProgressBridge, SettingsWidget; Методы и функции: check_mcp_servers, _value, _build, _scroll_page, _select_page
- **`GitPanel.py`** (1300 строк) — Классы: GitHubLoginDialog, GitHubReposDialog, CreateGitHubRepoDialog, CloneGitHubDialog; Методы и функции: _build, _toggle_echo_mode, _open_token_page, _do_login, _load_repos
- **`test_ai_and_scale.py`** (1295 строк) — Классы: Provider, View, _Handler, _Editor; Методы и функции: test_worker_delegates_read_only_project_subtask, converse, test_agent_search_returns_sources_and_keeps_workspace_active, page, test_agent_browser_navigation_and_async_page_text
- **`CodeLensBar.py`** (1168 строк) — Классы: CodeSymbol, CleanCodeRefactorer, DocumentationGenerator, CompactOutlinePopup; Методы и функции: format_code, describe_name, describe_param, _docstring, generate_function_docstring
- **`PDFViewerWidget.py`** (1105 строк) — Классы: PDFViewerWidget; Методы и функции: render_page_payload, search_document_payload, _build, _build_toolbar, _tool_button
- **`DatabaseWorkspace.py`** (1101 строк) — Классы: ServerProfileDialog, ResultModel, DatabaseManager, DatabaseWorkbench; Методы и функции: button, _kind_changed, _load, accept, set_data
- **`test_editor_and_terminal.py`** (964 строк) — Классы: Shell, StubWindow, Workbench; Методы и функции: test_terminal_shortcuts_take_precedence_and_filter_control_characters, is_run_process_active, interrupt, print_prompt, test_terminal_backspace_deletes_input_and_preserves_prompt
- **`project_intelligence.py`** (927 строк) — Классы: SymbolInfo, StructureNode, StructureEdge, Issue; Методы и функции: _decode_analyzer_result, _file_digest, _decode_security_findings, python_files, source_files
- **`TodoPanel.py`** (871 строк) — Классы: CheckCircleButton, StarButton, TaskCardWidget, TodoPanel; Методы и функции: _apply_style, _build_ui, _on_check_clicked, _on_star_clicked, _on_location_clicked

---

## 3. Диагностика качества кода и технический долг

| Код | Уровень | Локализация | Описание проблемы |
| :--- | :--- | :--- | :--- |
| `PY020` | **WARNING** | `top_bar.py:75` | Function has more than 80 top-level statements. |
| `PY021` | **WARNING** | `project_tasks.py:170` | Function has more than 7 parameters. |
| `PY021` | **WARNING** | `agent_tools.py:181` | Function has more than 7 parameters. |
| `PY021` | **WARNING** | `ai_service.py:917` | Function has more than 7 parameters. |
| `PY021` | **WARNING** | `team_cache.py:285` | Function has more than 7 parameters. |
| `PY020` | **WARNING** | `AgentTeamTab.py:48` | Function has more than 80 top-level statements. |
| `PY021` | **WARNING** | `AIAgentPanel.py:133` | Function has more than 7 parameters. |
| `PY020` | **WARNING** | `AIAgentPanel.py:1098` | Function has more than 80 top-level statements. |

> **Рекомендации по улучшению:**
> - Заменить обобщённые блоки перехвата `except:` на типизированные (например, `except (IOError, ValueError):`).
> - Провести декомпозицию длинных функций (>80 выражений) на компактные приватные подфункции.
> - Дополнить аннотации типов аргументов и возвращаемых значений для ключевых методов API.

---

## 4. Ключевые вопросы при Code Review / Архитектурном аудите

### 1. Архитектурная декомпозиция и расширяемость
> **Вопрос:** *Каким образом соблюдается принцип разделения ответственности (Single Responsibility)?*
> **Ответ:** *Проект разделен на слабосвязанные модули (`188` модулей). Логика изолирована по функциональным слоям, что упрощает масштабирование, модульное тестирование и внесение изменений без каскадных регрессий.*

### 2. Точка входа и управление жизненным циклом
> **Вопрос:** *Как структурирован запуск и завершение программы?*
> **Ответ:** *Выполнение стартует с `main.py`. Происходит валидация системного окружения, загрузка сохранённой конфигурации, запуск главного графического цикла и инициализация фонового потока индексации. При завершении программы ресурсы корректно высвобождаются.*

### 3. Обработка исключений и надёжность
> **Вопрос:** *Как реализована стратегия обработки непредвиденных ошибок?*
> **Ответ:** *Критические операции ввода-вывода, сетевых запросов и парсинга обернуты в типизированные блоки перехвата исключений `try-except` с логированием контекста ошибок, предотвращая аварийное падение процесса.*

### 4. Алгоритмическая и пространственная сложность
> **Вопрос:** *Какова вычислительная сложность ключевых операций обработки данных?*
> **Ответ:** *Итерационные проходы спроектированы со сложностью **O(N)**. Для частых операций поиска и сопоставления используются хэш-таблицы (словари и множества), обеспечивая амортизированную сложность выборки **O(1)**.*

### 5. Управление техническим долгом
> **Вопрос:** *Какие направления рефакторинга и оптимизации являются приоритетными?*
> **Ответ:** *Регулярное покрытие юнит-тестами, минимизация вложенности в алгоритмах, внедрение строгой типизации (type annotations) и кэширование повторных вычислений.*

---

## 5. Справочник архитектурных и алгоритмических терминов

- **Инкапсуляция (Encapsulation)** — изоляция внутреннего состояния и реализации объекта с предоставлением доступа только через публичный интерфейс.
- **Декомпозиция (Decomposition)** — разделение сложной системы на независимые, легко читаемые и тестируемые компоненты.
- **Цикломатическая сложность (Cyclomatic Complexity)** — число линейно независимых путей выполнения через код. Чем выше значение, тем сложнее код для тестирования и анализа.
- **Технический долг (Technical Debt)** — накопленные компромиссные решения и временные заглушки (TODO/FIXME), требующие планового устранения.
- **AST (Abstract Syntax Tree)** — древовидное представление синтаксической структуры исходного кода, применяемое компиляторами и статическими анализаторами.

---
*Сформировано в CodeLane IDE · Project Intelligence & Quality Assurance*
