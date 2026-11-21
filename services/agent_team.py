"""In-process coordination for a CodeLane agent team."""

from dataclasses import dataclass, field
import json
import time
from threading import RLock

from services.agent_delegation import ROLES, delegate
from services.agent_edits import EDIT_SCHEMA, EditCandidate, FileEdit
from services.agent_harness import AgentUsage
from services.ai_service import AIError
from services.team_scheduler import TeamScheduler
from services.team_optimizer import TeamOptimizer


def _tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _tool("assign_team", "Поручить независимые задания параллельно и получить результаты.",
          {"tasks": {"type": "array", "items": {"type": "object", "properties": {
              "role": {"type": "string", "enum": list(ROLES)},
              "task": {"type": "string"},
              "key": {"type": "string", "description": "Уникальное имя задачи внутри пакета."},
              "depends_on": {"type": "array", "items": {"type": "string"},
                             "description": "key задач этого пакета, чьи результаты нужны перед запуском."},
          }, "required": ["role", "task"]}}}, ["tasks"]),
    _tool("ask_user", "Запросить существенное уточнение, собранное агентом постановки.",
          {"question": {"type": "string"}}, ["question"]),
]


@dataclass
class TeamResult:
    answer: str = ""
    question: str = ""
    usage: dict = field(default_factory=dict)
    failed_agents: int = 0
    edits: list[FileEdit] = field(default_factory=list)
    complete: bool = True
    changed_files: list[str] = field(default_factory=list)
    optimization: dict = field(default_factory=dict)


def run_team(service, toolbox, goal, emit, stopped, *, capacity=None, parallelism=None, report=None, trace=None,
             scheduled_role=None, relationship=None):
    started = time.monotonic()
    learning_warning = ""
    try:
        optimizer = TeamOptimizer(getattr(toolbox, "team_cache", None), str(getattr(toolbox, "root", "")),
                                  str(getattr(service, "base_url", "")) + "|" + str(getattr(service, "model", "")))
    except Exception as exc:
        learning_warning = f"История оптимизации недоступна: {exc}"
        optimizer = TeamOptimizer()
    try:
        initial_plan = optimizer.recommend()
    except Exception as exc:
        learning_warning = f"История оптимизации недоступна: {exc}"
        optimizer = TeamOptimizer()
        initial_plan = optimizer.recommend()
    team_description = {}
    optimize_tool = _tool("optimize_team", "Рассчитать рекомендуемое число исполнителей по сложности и истории. "
                          "Вызывай после оценки задачи, до назначения команды; work_units — независимые части работы.",
                          {key: {"type": "number"} for key in (
                              "work_units", "parallel_fraction", "risk", "quality_weight", "time_weight", "cost_weight")},
                          ["work_units", "parallel_fraction", "risk"])
    assess_tool = _tool("assess_team", "Перед итогом запиши оценку состава команды. "
                        "Отделяй фактические проверки от предположений.",
                        {key: {"type": "string"} for key in ("description", "evidence", "inefficiencies", "next_structure")},
                        ["description", "evidence", "inefficiencies", "next_structure"])
    definitions = toolbox.definitions()
    from services.ide_mcp import IDEMCP
    ide = IDEMCP(toolbox)
    supervisor_tools = list(definitions)
    supervisor_names = {definition["function"]["name"] for definition in supervisor_tools}
    edits_enabled = any(definition["function"]["name"] == "edit_file" for definition in definitions)
    candidate = EditCandidate(toolbox)
    proposals = {}
    accept = _tool("accept_proposal", "Принять проверенную правку исполнителя по ID без пересылки текста.",
                   {"id": {"type": "string"}}, ["id"])
    reconcile = _tool("reconcile_edit", "Заменить пересекающиеся предложения единой согласованной правкой. "
                      "Исходный фрагмент должен полностью охватывать заменяемые правки.",
                      EDIT_SCHEMA["function"]["parameters"]["properties"], ["path", "old_text", "new_text"])
    usage = AgentUsage()
    question = ""
    assigned = 0
    failed_agents = 0
    requirements_ready = False
    requirements_assigned = False
    completed = {}
    published = set()
    mutation_lock = RLock()
    changed_files = set()
    from services.agent_tools import _CONFIRM_POLICY
    from services.workbench_tools import TOOLS as WORKBENCH_TOOLS

    def run_ide(name, payload):
        access = WORKBENCH_TOOLS.get(name, (None,))[0]
        if _CONFIRM_POLICY.get(name, False) or access in {"write", "command", "full"}:
            with mutation_lock:
                cache = getattr(toolbox, "team_cache", None)
                if cache is not None and name in {"edit_file", "write_file"}:
                    try:
                        target = toolbox._resolve(payload["path"])
                        if target.is_file():
                            path, before = toolbox.preview_edit_source(payload["path"])
                            if name == "write_file":
                                after = payload["content"]
                            else:
                                old = payload["old_text"]
                                if not old or before.count(old) != 1:
                                    return "Ошибка: исходный фрагмент должен встречаться ровно один раз."
                                after = before.replace(old, payload["new_text"], 1)
                            if after == before:
                                return "Файл уже содержит запрошенный текст."
                            edit = FileEdit(path, before, after)
                            message = cache.apply(str(toolbox.root), edit, toolbox,
                                                  getattr(toolbox, "team_run_id", None))
                            changed_files.add(path)
                            if trace:
                                trace(0, "Применено в фоне, снимок сохранён:\n" + edit.diff())
                            return message
                    except Exception as exc:
                        return f"Ошибка правки: {exc}"
                return ide.call(name, payload)
        return ide.call(name, payload)
    seen = set()

    def run_agent(agent_id, role, task, children, dependency_context):
        with mutation_lock:
            team_description[agent_id] = {"role": role, "task": task}
        agent_usage = AgentUsage()
        title = ROLES[role][0]
        if stopped():
            emit(agent_id, title, task, "Отменено")
            return None, agent_usage.as_dict()
        emit(agent_id, title, task, "Работает")
        try:
            options = {"collect_edits": True} if edits_enabled and role != "requirements" else {}
            if trace:
                options["trace"] = lambda text: trace(agent_id, text)
            options["assign_children"] = children
            options["tool_runner"] = run_ide
            if dependency_context:
                options["dependency_context"] = dependency_context
            result = json.loads(delegate(service, toolbox, definitions, {"role": role, "task": task},
                                         stopped, on_usage=agent_usage.add, **options))
        except Exception as exc:
            result = {"role": role, "error": str(exc)[:1000],
                      "retryable": isinstance(exc, AIError) and exc.retryable}
        status = "Отменено" if stopped() else "Ошибка" if "error" in result else "Готово"
        emit(agent_id, title, task, status)
        if report is not None and not stopped():
            report(agent_id, result.get("result") or result.get("error", ""))
        return result, agent_usage.as_dict()

    scheduler = TeamScheduler(run_agent, emit, stopped, capacity, parallelism, relationship)

    def route(name, args):
        nonlocal assigned, question, failed_agents, requirements_ready, requirements_assigned
        if stopped() or question:
            return "Запуск остановлен."
        if name == "optimize_team":
            try:
                return json.dumps(optimizer.recommend(**args), ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                return f"Ошибка параметров оптимизации: {exc}"
        if name == "assess_team":
            keys = ("description", "evidence", "inefficiencies", "next_structure")
            if any(not isinstance(args.get(key), str) or not args[key].strip() for key in keys):
                return "Заполни описание, доказательства, недостатки и предложение следующего состава."
            optimizer.assessment = {key: args[key] for key in keys}
            return "Оценка будет сохранена вместе с фактическими метриками запуска."
        if name in {"propose_edit", "reconcile_edit", "accept_proposal"} and edits_enabled and requirements_assigned:
            try:
                if name == "accept_proposal":
                    args = proposals.get(args.get("id"))
                    if args is None:
                        return "Неизвестный ID предложения."
                owners = {int(key.split(".")[0]) for key, edit in proposals.items() if edit == args}
                return candidate.add(args, reconcile=name == "reconcile_edit", agents=owners)
            except (ValueError, RuntimeError) as exc:
                return f"Ошибка кандидата: {exc}"
        if name in supervisor_names:
            payload = dict(args)
            return run_ide(name, payload)
        if name == "ask_user":
            value = args.get("question")
            if not requirements_assigned:
                return "Сначала назначьте агента постановки."
            if isinstance(value, str) and value.strip():
                question = value.strip()[:2000]
                return "Вопрос передан пользователю."
            return "Укажите вопрос."
        if name != "assign_team":
            return "Неизвестный инструмент."
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return "Укажите задания."
        if capacity is not None and len(tasks) > capacity - scheduler.assigned:
            return f"Укажите от 1 до {capacity - scheduler.assigned} заданий."
        batch = []
        for entry in tasks:
            role = entry.get("role") if isinstance(entry, dict) else None
            task = entry.get("task") if isinstance(entry, dict) else None
            if not isinstance(role, str) or role not in ROLES or not isinstance(task, str) or not task.strip():
                return "Укажите допустимую роль и цель."
            batch.append((role, task.strip()))
        if not requirements_assigned and sum(role == "requirements" for role, _ in batch) != 1:
            return "Первым пакетом назначьте одного агента постановки и независимые поручения."
        if assigned == 0 and scheduled_role and all(role != scheduled_role for role, _ in batch):
            return f"В первом пакете назначь запланированного агента с ролью {scheduled_role}."
        if len(set(batch)) != len(batch) or seen.intersection(batch):
            return "Уберите повторные задания или уточните цель повторной проверки."
        try:
            batch_results = scheduler.batch(tasks)
        except ValueError as exc:
            return f"Ошибка заданий: {exc}"
        seen.update(batch)
        requirements_assigned |= any(role == "requirements" for role, _ in batch)
        assigned = scheduler.assigned
        for number, (result, tokens) in sorted(scheduler.results.items()):
            if number not in completed:
                usage.add(tokens)
                failed_agents += int("error" in result)
                requirements_ready |= result["role"] == "requirements" and "result" in result
                for index, edit in enumerate(result.get("proposed_edits", []), 1):
                    key = f"{number}.{index}"
                    proposals[key] = edit
                    result.setdefault("proposal_ids", []).append(key)
                completed[number] = result
        batch_results = [completed[number] for number in sorted(completed)
                         if number not in published]
        published.update(completed)
        if any("error" in result for result in batch_results):
            return json.dumps({"results": batch_results, "next_batches":
                               "Сохрани успешные результаты. Переназначь только нужные незавершённые задачи.",
                               "parallelism": parallelism, "requirements_ready": requirements_ready},
                              ensure_ascii=False)
        return json.dumps(batch_results, ensure_ascii=False)

    def traced_route(name, args):
        if trace:
            trace(0, name + "\n" + json.dumps(args, ensure_ascii=False, indent=2))
        value = route(name, args)
        if trace:
            trace(0, value)
        return value

    emit(0, "Руководитель", goal, "Координирует")
    prompt = (
        "Ты единственный руководитель команды CodeLane. Сначала оцени объём, независимость частей и риск "
        "через optimize_team. Используй рекомендацию для числа агентов; отклонения обоснуй в assess_team. "
        "Подбирай минимальный достаточный состав под конкретный запрос. "
        "Для небольшой правки достаточно постановки, исполнителя и тестировщика. "
        "Не запускай каскады повторного ревью и исследования уже подтверждённых фактов. "
        "Субагентов создавай для самостоятельных частей, когда это экономит общее время и стоимость. "
        "После целевых тестов и одного ревью заверши задачу, если не найден воспроизводимый дефект. "
        "Первым вызовом assign_team отправь единый пакет: агент постановки и независимые задачи. "
        "Агент постановки формулирует критерии и вопросы. Если без ответа нельзя продолжить, "
        "вызови ask_user. Иначе сам выбери число, роли и разные конкретные цели исполнителей. "
        "Независимые задания отправляй одним пакетом. Для запроса о правках подключи тестировщика, "
        "Используй key и depends_on для зависимых задач внутри пакета: ядро дождётся их завершения "
        "и передаст результаты исполнителю. Исполнители могут создавать собственных субагентов, "
        "их результаты и предложения также доступны тебе по agent_id. "
        "для интерфейса — дизайнера. Перепроверь выводы, согласуй пересечения по файлам и символам, "
        "при необходимости поручай уточняющие проверки. Если доступен propose_edit, принимай "
        "готовые предложения исполнителей по ID через accept_proposal, собственные — через propose_edit. "
        "При пересечении прочитай оба предложения и отправь общий охватывающий фрагмент через reconcile_edit. "
        "Инструменты IDE доступны всем участникам. Для независимых изменений и проверок "
        "используй подходящие инструменты напрямую. Сообщай о фактически выполненных действиях. "
        "При сбое отдельного агента используй успешные результаты и прямое чтение проекта. "
        "Если агент постановки упал, сформулируй критерии из исходной цели и продолжи. "
        "Для веб-исследования найди страницы через browser_search, открой и прочитай источники и дай ссылки. "
        "Не проси пользователя выбирать способ повторного запуска команды. "
        "Перед итогом вызови assess_team: опиши состав, подтверждения результата, лишние затраты "
        "и что изменить в составе следующей похожей команды. Не выдавай самооценку за результаты тестов. "
        "Заверши общим результатом и укажи ограничения проверки.\n"
        "Начальная оценка (уточни под задачу): " + json.dumps(initial_plan, ensure_ascii=False) + "\n\nЦель:\n" + goal
    )
    error = ""
    try:
        answer = service.converse(
            prompt, tools=TOOLS + [optimize_tool, assess_tool] + supervisor_tools + ([EDIT_SCHEMA, accept, reconcile] if edits_enabled else []),
            tool_runner=traced_route, should_stop=lambda: stopped() or bool(question), unlimited_rounds=True,
        )
        usage.add(service.last_usage)
    except Exception as exc:
        answer, error = "", str(exc)
    edits = candidate.files() if not question and not stopped() else []
    reason = getattr(service, "last_stop_reason", None)
    incomplete = not question and not stopped() and (
        bool(error) or reason in {"empty", "limit"} or not answer.strip() or not requirements_assigned
        or failed_agents > 0
    )
    if incomplete:
        detail = error or {
            "limit": "Исчерпан лимит раундов руководителя.",
            "empty": "Модель руководителя вернула пустой ответ.",
        }.get(reason, "Есть незавершённые задания агентов." if failed_agents else
              "Руководитель не завершил работу команды.")
        lines = [f"**Частичный итог.** {detail}", f"Файлов в подготовленных правках: {len(edits)}."]
        if answer.strip() and not error and reason not in {"empty", "limit"}:
            lines.append(answer)
        for number, result in sorted(completed.items()):
            title = ROLES[result["role"]][0]
            summary = result.get("error") or result.get("result", "")
            lines.append(f"- **{title} · {number}** — {' '.join(summary.split())[:280]}")
        answer = "\n\n".join(lines)
    status = ("Отменено" if stopped() else "Ждёт ответа" if question else
              "Частичный итог" if incomplete else "Готово")
    emit(0, "Руководитель", goal, status)
    outcome = "cancelled" if stopped() else "question" if question else "failed" if incomplete else "complete"
    agents = [{"id": number, **team_description.get(number, {}),
               "role": result.get("role"), "task": result.get("task", team_description.get(number, {}).get("task", "")),
               "dependencies": result.get("dependencies", []),
               "parent_id": result.get("parent_id", 0), "error": result.get("error"),
               "child_errors": result.get("child_errors", [])}
              for number, (result, _) in sorted(scheduler.results.items())]
    try:
        experience = optimizer.record(goal=goal, agents=agents, seconds=time.monotonic() - started,
                                      usage=usage.as_dict(), outcome=outcome)
    except Exception as exc:
        experience = {"plan": optimizer.plan}
        learning_warning = f"Не удалось сохранить опыт команды: {exc}"
    if not optimizer.assessment:
        experience["assessment_missing"] = True
    if learning_warning:
        experience["warning"] = learning_warning
        answer += "\n\n" + learning_warning
    if trace:
        trace(0, "Оптимизация команды:\n" + json.dumps(experience, ensure_ascii=False, indent=2))
    return TeamResult(answer, question, usage.as_dict(), failed_agents, edits, not incomplete,
                      sorted(changed_files), experience)
