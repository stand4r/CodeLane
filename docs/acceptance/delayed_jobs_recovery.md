# Критерии приёмки: восстановление отложенных заданий после аварийного закрытия

Статус: **реализовано** (код есть и покрыт автотестами; расхождения имплементации с
критериями ниже перечислены в §6 и не выдаются за сделанное).
Область: отложенные задания («Отложенные агенты», `AgentPlanner`) и их хранилище
`TeamCache` (`%LOCALAPPDATA%/<App>/agent-team/team.sqlite3`).
Ссылки «файл:строка» — фактические, по текущему коду; сверка выполнена по
`services/team_cache.py`, `core/job_recovery.py`, `app/application.py`, `widgets/AgentPlanner.py`
и тестам из §4.

## 1. Карта реализации (фактические точки)

| Точка | Что там |
|---|---|
| `services/team_cache.py:16` | `STALE_AFTER = 90` — порог «мёртвого» владельца (переопределяется аргументом) |
| `services/team_cache.py:19` | `REASON_INTERRUPTED` — текст причины; отдельного `reason_code` нет (Q7) |
| `services/team_cache.py:25` | `process_token()` — `pid-uuid4hex`, считается один раз на процесс |
| `services/team_cache.py:34` | `TeamCache(directory, owner=None, clock=None)`; `clock` — штатная точка подмены времени в тестах |
| `services/team_cache.py:53` | схема `jobs` (8 исходных колонок; новые добавляются миграцией) |
| `services/team_cache.py:61` | `ALTER TABLE snapshots ADD COLUMN run` (образец аддитивной миграции) |
| `services/team_cache.py:63-67` | аддитивная миграция `jobs`: `owner`, `started_at`, `heartbeat`, `reason` |
| `services/team_cache.py:68-71` | таблица `instances(token,pid,host,started,heartbeat,process_created)` |
| `services/team_cache.py:72-73` | retention 14 суток только для `snapshots`/`journals` (см. §6а) |
| `services/team_cache.py:83` | `heartbeat()` — upsert своего `instances` + heartbeat своих `running`-заданий |
| `services/team_cache.py:92`, `:96` | запись `instances` и `UPDATE jobs SET heartbeat=? WHERE state='running' AND owner=?` |
| `services/team_cache.py:99`, `:114` | `alive()/_alive()`: для своего хоста — `psutil` (pid + `process_created`), иначе freshness heartbeat |
| `services/team_cache.py:129-160` | `recover(stale_after=None, project=None) -> int` — идемпотентное восстановление |
| `services/team_cache.py:148-149` | выборка кандидатов: только `state='running'` и чужой `owner` (см. §6в) |
| `services/team_cache.py:157` | `UPDATE jobs SET state='paused',reason=?` c CAS по `id/project/state/owner` |
| `services/team_cache.py:162`, `:171` | `claim()/_claim()` — CAS-захват `queued → running` (и `paused → running`) с `owner/started_at/heartbeat` |
| `services/team_cache.py:281` | `jobs(project)` |
| `services/team_cache.py:285` | `save_job(..., job=None)`; сохранение существующего задания = повторный запуск (чистит `reason/owner/started_at/heartbeat`) |
| `services/team_cache.py:304-313` | `remove_job()` — `ValueError` только для `running` с живым владельцем (текст — см. §6ж) |
| `services/team_cache.py:315-349` | `state(project, job, state, expected=None, reason=None)` — `running` делегируется в `_claim`, `queued` сбрасывает владельца |
| `core/job_recovery.py:19` | `cache_dir()` — общий путь хранилища |
| `core/job_recovery.py:26` | `cache()` — общий экземпляр `TeamCache` |
| `core/job_recovery.py:34-44` | `recover_interrupted(project=None)` — тонкая обёртка над `TeamCache.recover` |
| `core/job_recovery.py:47` | `_beat()` — heartbeat без исключений в слот Qt |
| `core/job_recovery.py:55` | `start_heartbeat(parent, interval_ms=15000)` — 15 с |
| `app/application.py:234-243` | восстановление и heartbeat на старте GUI (`recover_interrupted()`, `start_heartbeat(app)`), оба вызова в `try/except` |
| `widgets/AgentPlanner.py:16` | `STATES` — подписи `queued/running/done/paused` |
| `widgets/AgentPlanner.py:19` | `PAUSED_INTERRUPTED = "Приостановлено · прервано"` (см. §6г) |
| `widgets/AgentPlanner.py:24` | `cache_dir()` — делегат `core.job_recovery.cache_dir` |
| `widgets/AgentPlanner.py:28`, `:90` | конструктор и таймер планировщика: `refresh` каждые 5 с |
| `widgets/AgentPlanner.py:93`, `:110` | `_reload` (дешёвое чтение без recover) и `_redraw` (дерево) |
| `widgets/AgentPlanner.py:134` | tooltip пункта: `prompt` + `Причина: <reason>` |
| `widgets/AgentPlanner.py:145` | `_reason(job)` — причина или `None` |
| `widgets/AgentPlanner.py:152-156` | `_status(job)` — `paused` с причиной помечается `PAUSED_INTERRUPTED` |
| `widgets/AgentPlanner.py:158-166` | `_restore_interrupted()` — `heartbeat()` + `recover()` перед каждой перерисовкой |
| `widgets/AgentPlanner.py:169-192` | `refresh()` — восстановление, перечитывание, захват `queued` |
| `widgets/AgentPlanner.py:177-184` | берутся только `queued` с наступившим `due`, `claim()` и один `team.submit` |
| `widgets/AgentPlanner.py:194-197` | `finished()` — `running → done` / `running → paused` |
| `widgets/AgentPlanner.py:249-266` | диалог: для живого `running` блокируются «Сохранить» и «Удалить задание», tooltip различает чужой экземпляр |
| `widgets/AgentPlanner.py:296-307` | кнопка «Запустить снова» для `paused` (`state(..., "queued", "paused")`) |

## 2. Публичный API (фактические имена)

```python
TeamCache(directory, owner=None, clock=None)
heartbeat() -> float
alive(token, stale_after=None) -> bool
recover(stale_after=None, project=None) -> int        # в постановке назывался recover_jobs
claim(project, job) -> bool                            # queued -> running (+owner, started_at, heartbeat)
state(project, job, state, expected=None, reason=None) -> bool
save_job(project, title, role, prompt, due, required_file, job=None)
remove_job(project, job) -> bool                       # ValueError — живой чужой владелец
jobs(project) -> list[sqlite3.Row]
```

Правило перехода при старте реализовано так: `running` + чужой `owner`, который не жив
→ `paused` + `REASON_INTERRUPTED`; `running` + живой чужой владелец → не трогаем;
`running` + наш собственный `owner` → не трогаем даже при протухшем heartbeat
(`services/team_cache.py:114-127`, тесты §4).

## 3. Критерии приёмки (проверяемые утверждения)

Легенда: ✅ выполнено, ⚠ расхождение/частично (подробности в §6).

### AC-1. Восстановление после сбоя
1. ✅ Сразу после старта GUI (до первого тика планировщика, 5 с) в БД нет `running` без
   живого владельца — `app/application.py:234-243`, `recover_interrupted()`.
2. ⚠ Такая строка переходит ровно в `paused`, `reason` непустой и содержит понятный текст
   (`REASON_INTERRUPTED`, `services/team_cache.py:19`). Поля `reason_code` нет вовсе — Q7
   оставил один текстовый `reason`, поэтому проверка `reason_code == "interrupted"` из
   исходной постановки снята; тесты проверяют `reason == REASON_INTERRUPTED`.
3. ✅ Идемпотентность: повторный `recover` возвращает `0` и не меняет `state/reason/id`
   (`tests/test_job_recovery.py:54`, повторный вызов внутри теста).
4. ✅ Восстановление не зависит от открытого проекта: `recover()` без `project`
   обрабатывает все проекты общей БД (`services/team_cache.py:152`, `tests/test_job_recovery.py:177`).
5. ⚠ Сбой во время восстановления не оставляет «полувосстановленных» строк (один
   `BEGIN IMMEDIATE` + CAS, `services/team_cache.py:147-160`), но трактовка `NULL`/
   неизвестного состояния как `paused` с `reason_code == "unknown"` **не реализована и
   отменена** (Q7). Побочный дефект: задания с `state IS NULL`/`''` не восстанавливаются
   вообще — фильтр `services/team_cache.py:148` берёт только `state='running'` (см. §6в).
6. ✅ Старые БД без новых колонок открываются без потери данных; миграция аддитивная
   (`services/team_cache.py:61-71`), повторный старт безопасен
   (`tests/test_job_recovery.py:200`).
7. ✅ `queued` и `done` восстановление не меняет — выборка ограничена `state='running'`.

### AC-2. Повторный запуск (retry)
8. ✅ Из `paused` доступен перезапуск: `state(project, id, "queued", "paused") is True`,
   `reason` очищается, `owner/started_at/heartbeat` сбрасываются (`services/team_cache.py:336-338`).
9. ✅ Повторный вызов для того же задания возвращает `False` (one-shot сохранён,
   `services/team_cache.py:171-181`, `tests/test_job_recovery.py:121`).
10. ✅ После перезапуска планировщик берёт задание на ближайшем тике (≤ 5 с) при простое
    команды и переводит в `running` с текущим `owner` (`widgets/AgentPlanner.py:169-184`).
11. ✅ Перезапуск не создаёт дубликат: `COUNT(*) == 1`, `title/role/prompt/due` сохраняются
    (`tests/test_job_recovery.py:121`).
12. ✅ Завершение работает как раньше: успех → `done`, отмена/ошибка → `paused`
    (`widgets/AgentPlanner.py:194-197`, `tests/test_team_cache.py:145`).

### AC-3. Удаление
13. ✅ `paused`-задание удаляется, журналы и снимки остаются (`services/team_cache.py:304-313`).
14. ✅ После восстановления «зависшее» задание удаляется без ручных правок БД.
15. ⚠ `running` с живым чужим владельцем: удаление запрещено и в хранилище (`ValueError`),
    и в UI (`tests/test_agent_planner_delete.py:115`), но текст ошибки хранилища —
    «Задание выполняется и его нельзя удалить.» (`services/team_cache.py:312`) — он **не
    называет причину** (другой экземпляр), как требует AC-15 и решение Q8. Чужой
    экземпляр упоминается только в tooltip UI (`widgets/AgentPlanner.py:265`).
16. ✅ `running` без живого владельца после старта не существует (AC-1), отдельного
    «удалить running» не требуется.
17. ✅ Чужой проект по-прежнему не удаляется: `remove_job` возвращает `False`
    (`tests/test_team_cache.py:84`, `tests/test_agent_planner_delete.py:132`).

### AC-4. Не трогать реально работающий экземпляр
18. ✅ Живой чужой владелец не меняется, в счётчик не попадает (`tests/test_job_recovery.py:82`).
19. ✅ Свежесть — по UTC-времени БД и условным `UPDATE ... WHERE state='running' AND owner=?`;
    гонка «восстановление против захвата» сериализуется `BEGIN IMMEDIATE`
    (`services/team_cache.py:147`, `tests/test_job_recovery.py:285`).
20. ✅ Захват и запись `owner/heartbeat` идут одним `UPDATE` (`services/team_cache.py:179`).

### AC-5. Несколько проектов
21. ✅ Восстановление по всем проектам, выборка `jobs(project)` не смешивается
    (`tests/test_job_recovery.py:177`).
22. ✅ Дерево показывает только открытый проект, причина видна в tooltip
    (`widgets/AgentPlanner.py:134`, `tests/test_agent_planner_search.py:218`).
23. ✅ Адресация только по `(project, id)`.
24. ✅ Задание неоткрытого проекта остаётся `paused` и само не запускается
    (`tests/test_job_recovery.py:177`, `tests/test_agent_planner_delete.py:132`).

### AC-6. UI и сообщения
25. ⚠ Статус приостановленного задания и причина в tooltip есть, но подпись —
    «Приостановлено · прервано» (`widgets/AgentPlanner.py:19`), что расходится и с AC-25
    («Приостановлено»), и с решением Q4 («Приостановлено · прерванное»); см. §6г.
26. ⚠ Для `paused` доступны «Сохранить», «Удалить задание» и «Запустить снова»
    (`widgets/AgentPlanner.py:296-307`) — третья кнопка названа иначе, чем «Перезапустить»
    в AC-26; для живого `running` «Сохранить» и «Удалить» заблокированы с объясняющим
    tooltip (`widgets/AgentPlanner.py:249-266`) — это соответствует.
27. ⚠ Уведомление о восстановлении один раз на старт (AC-27/T12) **не реализовано**:
    есть только `logger.info` (`app/application.py:237-238`), в UI ничего не показывается.
    Это противоречит T12, но согласовано решением Q11 (достаточно причины в списке и
    диалоге) — считать AC-27 снятым по Q11, а не выполненным.
28. ✅ Восстановление — короткая транзакция SQLite без сети, GUI не блокирует.

### AC-7. Не регрессировать
29. ✅ `tests/test_team_cache.py`, `tests/test_agent_planner_delete.py`,
    `tests/test_job_recovery.py`, `tests/test_agent_planner_search.py` проходят
    (`py -3.14 -m pytest -q …` → 38 passed).
30. ✅ `restore`, `restore_run`, `apply` контракт не меняли, новых обязательных параметров нет.
31. ✅ Задания старой версии (`queued/done/running` без `owner`) читаются; `running` без
    `owner` трактуется как прерванное (Q5, `tests/test_job_recovery.py:200`).

## 4. Сопоставление сценариев T1–T12 с фактическими тестами

| ID | Сценарий из постановки | Фактический тест | Состояние |
|---|---|---|---|
| T1 | Восстановление после сбоя | `tests/test_job_recovery.py:54` `test_interrupted_job_becomes_paused_with_reason_and_deletes` | покрыт (`recover() == 1`, `paused`, `REASON_INTERRUPTED`) |
| T2 | Идемпотентность | тот же тест (`tests/test_job_recovery.py:54`, повторный `recover() == 0` внутри) | покрыт |
| T3 | Живой чужой экземпляр | `tests/test_job_recovery.py:82` `test_live_foreign_instance_keeps_running_and_blocks_delete` | покрыт, кроме текста `ValueError` про другой экземпляр |
| T4 | Повторный запуск | `tests/test_job_recovery.py:121` `test_retry_clears_reason_and_claims_once` | покрыт |
| T5 | Запуск планировщиком | `tests/test_team_cache.py:145` `test_planner_waits_for_both_conditions_and_runs_once`, `tests/test_agent_planner_search.py:197` `test_hidden_by_filter_job_still_runs` | частично: проверен захват `queued` и `submit` один раз; нет теста «`paused` → перезапуск → планировщик подхватил ≤ 5 с» |
| T6 | Удаление после восстановления | `tests/test_job_recovery.py:54` (хвост), `tests/test_job_recovery.py:148` | покрыт |
| T7 | Удаление живого | `tests/test_job_recovery.py:82`, `tests/test_agent_planner_delete.py:115` `test_running_job_delete_is_disabled_and_store_rejects_it` | покрыт (кнопка disabled + `ValueError`), текст ошибки не проверяется |
| T8 | Несколько проектов | `tests/test_job_recovery.py:177` `test_recover_is_project_scoped_then_global` | покрыт (скоуп, глобальный вызов, изоляция `jobs(project)`) |
| T9 | Закрытый проект | `tests/test_agent_planner_delete.py:132` `test_foreign_project_job_is_hidden_and_survives_delete`, `tests/test_agent_planner_search.py:218` | частично: проверено «чужое не видно/не удаляется»; нет проверки «`team.submit` для чужого проекта не вызывался» |
| T10 | Миграция старой БД | `tests/test_job_recovery.py:200` `test_old_database_without_new_columns_is_migrated` | покрыт (в тесте старая строка `running`, а не `queued`) |
| T11 | Неизвестное состояние | — | **пробел и отмена**: `reason_code` не вводился (Q7), `recover` не смотрит `state IS NULL/''` (§6в); есть только `tests/test_team_cache.py:98` `test_remove_job_deletes_row_with_empty_state` (такая строка удаляема вручную) |
| T12 | Метка и причина в UI, уведомление один раз | — | **пробел**: нет теста на «Приостановлено · прервано», на tooltip с причиной и на кнопку «Запустить снова»; уведомление не реализовано (Q11) |

Дополнительные автотесты вне таблицы T1–T12 (покрывают AC-4/AC-19):
`tests/test_job_recovery.py:234` `test_heartbeat_tracks_instance_liveness`,
`:253` `test_real_process_exit_is_recovered_without_waiting_for_heartbeat`,
`:268` `test_live_process_is_protected_when_heartbeat_expires`,
`:285` `test_delete_and_claim_are_serialized`,
`:104` `test_own_running_job_survives_recovery`.

`tests/test_agent_team.py` восстановление заданий не проверяет (это тесты делегирования и
супервизора); для этой задачи релевантен только косвенно — через `team.submit`.

Команды: `py -3.14 -m pytest -q tests/test_job_recovery.py tests/test_team_cache.py
tests/test_agent_planner_delete.py tests/test_agent_planner_search.py` и `pytest -q` перед сдачей.

## 5. Открытые вопросы: закрыты решениями

| Вопрос | Ответ |
|---|---|
| 1. Идентификатор владельца | Токен `pid-uuid4hex` (`services/team_cache.py:25`) + таблица `instances` (`:68-71`); своя машина проверяется через `psutil`, чужая — по heartbeat. Смена hostname/OneDrive-профиля ловится как «мёртвый владелец» — принято как ограничение. |
| 2. Heartbeat | 15 с (`core/job_recovery.py:55`, `app/application.py:242`), порог `STALE_AFTER = 90` (`services/team_cache.py:16`), переопределяется `stale_after`. |
| 3. Где восстанавливать | Оба места: глобально на старте (`app/application.py:234-243`) и на каждом `refresh` (`widgets/AgentPlanner.py:158-166`). |
| 4. Состояние или `paused`+`reason` | `paused` + `REASON_INTERRUPTED`, новых состояний нет; см. §6г про текст подписи. |
| 5. Совместимость со старыми сборками | Миграция только аддитивная; `running` без `owner` = прерванное (Q5). |
| 6. Автоперезапуск | Не выполняется: `refresh` берёт только `queued` (`widgets/AgentPlanner.py:177`), `paused` требует ручного «Запустить снова». |
| 7. Тексты причин | Один текстовый `reason`, без словаря кодов → AC-1.2/1.5 и T11 в части `reason_code` сняты. |
| 8. Реальность «другого экземпляра» | Определяется `instances`, а не `SingleInstance`; живой чужой владелец защищён (`services/team_cache.py:114-127`). Текст `ValueError` из Q8 не реализован — §6ж. |
| 9. Смерть воркера без исключения | Отдельного кода нет: heartbeat принадлежит процессу (осознанное ограничение). |
| 10. Очистка | Задания живут до ручного удаления; чистка `instances` старше 7 суток **не реализована** — §6а. |
| 11. Уведомление | Отдельного бейджа/сообщения нет, только причина в списке и диалоге → T12/AC-27 сняты. |

## 6. Расхождения имплементации с критериями (не сделано / сделано иначе)

- **(а) Нет очистки `instances` старше 7 суток (Q10).** В `TeamCache.__init__` чистятся
  только `snapshots` и `journals` (`services/team_cache.py:72-73`); строки `instances`
  (в том числе от старых профилей и токенов) растут без ограничения. Требование Q10
  выполнено наполовину: задания действительно живут до ручного удаления.
- **(б) Уведомление о восстановлении один раз (T12/AC-27) не реализовано** и противоречит
  Q11: на старте есть только `logger.info` (`app/application.py:237-238`), в UI ничего не
  появляется; теста нет. Q11 («достаточно причины в списке и диалоге») отменяет T12 —
  расхождение считается согласованным, но не выполненным.
- **(в) `reason_code`/`unknown` (AC-1.2, AC-1.5, T11) отменены Q7**, плюс остаётся
  дефект: задания с `state IS NULL` или `''` никогда не восстанавливаются — фильтр
  `services/team_cache.py:148` берёт только `state='running'`. Такие строки либо висят в
  дереве как «Неизвестно», либо удаляются вручную (`tests/test_team_cache.py:98`).
- **(г) Текст «Приостановлено · прервано» (`widgets/AgentPlanner.py:19`) против спеки:**
  AC-25 требует «Приостановлено», решение Q4 — «Приостановлено · прерванное»; тестов на
  подпись нет.
- **(д) Восстановление не вызывается в headless-пути `app/agent_workbench.py`.**
  `core.job_recovery` используется только в GUI (`app/application.py:234-243`) и
  планировщике (`widgets/AgentPlanner.py:7,158-166`); в `app/agent_workbench.py` нет ни
  `TeamCache`, ни `heartbeat`, ни `recover` — headless-запуск не восстанавливает
  прерванные задания и не подтверждает жизнь экземпляра.
- **(е) Ручной тест аварийного закрытия GUI не выполнен.** Автотесты покрывают уровень
  `TeamCache` (11 тестов в `tests/test_job_recovery.py`), offscreen-планировщик и
  подпроцессный выход процесса; реальное «убить процесс GUI и перезапустить», второй
  экземпляр на другой машине в сетевой папке и поведение на Windows проверены только
  логически.
- **(ж) Текст ошибки хранилища не называет причину.** AC-15 и решение Q8 требуют
  «Задание выполняет другой экземпляр CodeLane: <token>», в коде —
  «Задание выполняется и его нельзя удалить.» (`services/team_cache.py:312`).
- **(з) Имя кнопки перезапуска.** AC-26 говорит «Перезапустить», в коде — «Запустить
  снова» (`widgets/AgentPlanner.py:301`).
- **(и) Число тестов в разделе «Ограничения проверки» устарело:** в
  `tests/test_job_recovery.py` не 8, а 11 тестов.

## 7. Как проверить

```
py -3.14 -m pytest -q tests/test_job_recovery.py tests/test_team_cache.py tests/test_agent_planner_delete.py tests/test_agent_planner_search.py
py -3.14 -m pytest -q
```

Grep-проверки документа (выполнены, результат ниже):

```
# 1) статус наверху — «реализовано», а не «постановка»
py -3.14 -c "import pathlib;t=pathlib.Path('docs/acceptance/delayed_jobs_recovery.md').read_text(encoding='utf-8');print([l for l in t.splitlines() if l.startswith('Статус')])"
# 2) в таблице §4 стоят имена реальных тестов
py -3.14 -c "import pathlib;t=pathlib.Path('docs/acceptance/delayed_jobs_recovery.md').read_text(encoding='utf-8');print(t.count('test_'))"
# 3) старое имя recover_jobs встречается только как пометка «в постановке назывался»
py -3.14 -c "import pathlib;t=pathlib.Path('docs/acceptance/delayed_jobs_recovery.md').read_text(encoding='utf-8');print([l for l in t.splitlines() if 'recover_jobs' in l])"
```

Факт: пункт 1 — `Статус: **реализовано** …`; пункт 2 — 60+ вхождений `test_`, включая
все 12 строк таблицы §4; пункт 3 — старое имя осталось только в §2 (пометка «в постановке
назывался») и в самой команде проверки.
