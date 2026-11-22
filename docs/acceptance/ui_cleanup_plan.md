# Карта правок UI и критерии проверки (3 пункта)

Составлено по факту чтения кода (строки актуальны на момент составления). Правки ещё не внесены.

## 1. Удаление поиска в «Отложенные агенты»

**widgets/AgentPlanner.py**
- `:49-54` — удалить создание `self.search = QLineEdit()` и настройки (`objectName("agentPlannerSearch")`, placeholder, clearButton, tooltip, `textChanged.connect(self._apply_filter)`).
- `:73` — удалить `box.addWidget(self.search)` (тулбар остаётся: свернуть / Журналы / Снимки / +).
- `:99-108` — удалить методы `_apply_filter` и `_matches` целиком.
- `:110-135` `_redraw` — убрать `query = self.search.text()...` (`:114`), `visible = [...]` (`:115`) → итерировать `jobs`; сигнатура `:116` без `query` (`(root, tuple(tuple(j) for j in jobs))`); `:120` текст подписи всегда `"Нет запланированных задач"` (ветка «Ничего не найдено» исчезает); `:121` `setVisible(not jobs)`; `:123` `for job in jobs`.
- `:137-141` `_toggle_collapsed` — убрать `self.search.setVisible(not collapsed)` (`:140`), оставить только скрытие `self.tree`.

**design/theme.qss**
- `:1227-1233` — удалить селекторы `QLineEdit#agentPlannerSearch` и `QLineEdit#agentPlannerSearch:focus`. Остальные блоки (`#agentPlanner`, `#agentPlannerTree`, `#agentPlannerButton`, `#agentPlannerAdd`) не трогать.

**Критерии приёмки**
- `search_in_project("agentPlannerSearch")` → 0 совпадений (кроме `docs/`, где фиксируем решение по документам).
- `AgentPlanner` не имеет атрибута `search`: `assert not hasattr(panel.planner, "search")`.
- Дерево показывает все задания проекта: 3 задания (2 «своих», 1 чужой проект) → в дереве 2 элемента без каких-либо манипуляций с фильтром; задания чужого проекта по-прежнему не видны.
- При пустом хранилище видна подпись «Нет запланированных задач»; при непустом `empty_label.isVisible() is False`.
- Сворачивание/разворачивание работает, таймер `refresh` не останавливается (`panel.planner.timer.isActive()`).
- Запуск: `python -m pytest tests/test_agent_planner_delete.py -q` — зелёный.

## 2. Крупный глиф «+» при неизменном размере кнопки

Факт: глиф — это текст `"+"` (`widgets/AgentPlanner.py:68`), `setIconSize`/`setFixedSize` не используются; габарит задаёт QSS `design/theme.qss:1241-1245` (`padding: 5px 9px; min-height: 18px`). Значит «просто увеличить font-size» увеличит и кнопку — размер нужно зафиксировать.

**widgets/AgentPlanner.py `:66-71`**
- После QSS-метрик задать явный размер кнопки (например `add.setFixedSize(28, 28)` — сверить с фактическим `add.size()` до правки и взять те же числа) и увеличить глиф: `QFont` с `pointSizeF * 1.5` + `setBold(True)`, либо иконка-плюс, нарисованная в `QPixmap` с `setIconSize(QSize(18, 18))` и `setText("")`.
- Не переопределять `padding`/`min-height` для `#agentPlannerAdd` в QSS (это и есть текущий размер).

**Критерии приёмки**
- `add.size()` и `add.sizeHint()` после правки равны значениям до правки (замер offscreen до/после: ожидаемо 28×28, зафиксировать фактические).
- Высота тулбара не изменилась: `row.sizeHint().height()` до == после.
- Глиф визуально крупнее: `add.font().pointSizeF() >= 1.4 * базового`, либо `add.iconSize().height() >= 16` при пустом тексте. Замер через offscreen-скриншот `add.grab()` — знак занимает ≥60 % высоты кнопки.
- Клик по кнопке по-прежнему открывает диалог создания: `add.click()` → появился диалог/`planner.edit()` вызван.

## 3. Удаление пункта «ИИ-агент» из ActivityBar

**widgets/ActivityBar.py**
- `:67` — удалить элемент `("ai", "ai-agent", "ИИ-агент (Ctrl+Shift+I)")` из `ITEMS`.
- `:261-266` — удалить особую ветку `if key == "ai":` в `_activate_or_toggle`.
- `:289-290` — удалить ветку `if key == "ai": self.activity_changed.emit(key)` в `set_activity` (иначе осиротевший путь).

**app/main_window.py**
- `:1021-1030` `show_ai_agent` — убрать `activity_bar.set_activity("ai")` и fallback `_handle_activity("ai")`; вызывать напрямую `self.ai_panel._open_agent_team()` (это и делала ветка `_handle_activity`, `:1089-1092`), сохранив проверку `extension_enabled("ai")`.
- `:1087` — убрать `"ai"` из набора `{"ai", "todo", "git", "intelligence"}` в `_handle_activity`.
- `:1089-1092` — удалить блок `if key == "ai": ...`.
- `:2120-2123` `apply_builtin_extensions` — цикл `activity.buttons[key].setVisible(visible)` даст `KeyError: 'ai'` после удаления из `ITEMS`; оставить `ai` только в `enabled` для `self.top_bar.agent_button.setVisible(enabled["ai"])` (`:2121`), а итерацию по кнопкам ограничить существующими ключами (`for key in activity.buttons` / фильтр `if key in activity.buttons`).
- Не трогать: `app/top_bar.py:168-176` (`agent_button` — новый вход), `utils/settings_manager.py:11` (ключ расширения `ai` остаётся), `:2132-2133` (`clear_queue` по настройке расширения), вызовы `show_ai_agent()` из `widgets/ProjectIntelligenceDashboard.py:1634` и `widgets/SettingsWidget.py:1161`.

**Критерии приёмки**
- `"ai"` отсутствует в `ActivityBar.ITEMS`; `"ai" not in bar.buttons`; число кнопок уменьшилось на 1; у остальных кнопок бейджи/индикатор работают.
- `bar.set_activity("ai")` и `bar._activate_or_toggle("ai")` не бросают исключений и ничего не открывают.
- `window.show_ai_agent()` открывает вкладку/панель агента (поведение как раньше), `window.top_bar.agent_button` виден при `extension_ai=True` и скрыт при `False`.
- `window.apply_builtin_extensions()` выполняется без `KeyError` при любых комбинациях настроек расширений.
- `search_in_project("set_activity(\"ai\")")` и `search_in_project("_handle_activity(\"ai\")")` → 0 совпадений.
- Запуск: `python -m pytest tests/test_ui_responsiveness.py -q` и `python main.py` — в баре нет значка ИИ, кнопка «Агент» сверху открывает агента.

## Решение по тестам и документации

- `tests/test_agent_planner_search.py` (286 строк, 11 тестов, все используют `planner.search`) — **удалить файл**. Перед удалением перенести в `tests/test_agent_planner_delete.py` 4 не-поисковых сценария, которые иначе потеряют покрытие: `test_no_jobs_at_all_shows_scheduled_empty_label` (`:183`), `test_collapse_with_active_filter_hides_tree_but_keeps_timer` (`:271`, переписать без `search`), `test_foreign_project_jobs_never_match_filter` (`:218`, переписать как «задания чужого проекта не видны»), `test_filtered_job_opens_edit_dialog_with_full_data` (`:232`) и `test_filtered_job_can_be_deleted_from_tree` (`:251`, без установки фильтра). Фикстура `panel` и хелперы уже есть в `tests/test_agent_planner_delete.py:18-87` — копировать не нужно.
- `docs/acceptance/agent_planner_search.md` — **не удалять, а пометить как снятый**: заменить статус на «Отменено: функциональность поиска удалена из UI (см. `docs/acceptance/ui_cleanup_plan.md`)», сохранив историю требований; ссылку на `design/theme.qss:1227-1246` (`:120` документа) пометить недействительной.
- Проверка после правок: `python -m pytest tests -q -k "agent_planner"` — зелёный; `search_in_project("planner.search")` → только `tests/test_ui_responsiveness.py:57` (это `SearchPanel`, не планировщик).
