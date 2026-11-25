"""In-process coordination for a CodeLane agent team."""

from copy import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from threading import RLock

from services.agent_delegation import ROLES, delegate
from services.agent_edits import EDIT_SCHEMA, EditCandidate, FileEdit
from services.agent_harness import STALLED_NOTICE, AgentUsage
from services.ai_budget import clip_utf8
from services.ai_service import AIError, AIService
from services.task_evidence import TaskEvidence
from services.team_scheduler import TeamScheduler
from services.team_optimizer import TeamOptimizer
from services.agent_process import close_task_commands


def _tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


def _optimizer_enabled(service) -> bool:
    """Оптимизация команды (optimize_team/assess_team и запись опыта) выключена по умолчанию.

    Включается только явной настройкой ``ai_team_optimizer``: подбор размера
    команды и запись опыта не должны влиять на обычный запуск.
    """
    config = getattr(service, "config", None)
    if not isinstance(config, dict):
        return False
    value = config.get("ai_team_optimizer", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "да"}
    return value is True


TOOLS = [
    _tool("assign_team", "Поручить независимые задания параллельно и получить результаты.",
          {"tasks": {"type": "array", "items": {"type": "object", "properties": {
              "role": {"type": "string", "enum": list(ROLES)},
              "task": {"type": "string"},
              "key": {"type": "string", "description": "Уникальное имя задачи внутри пакета."},
              "depends_on": {"type": "array", "items": {"type": "string"},
                             "description": "key задач этого пакета, чьи результаты нужны перед запуском."},
          }, "required": ["role", "task"]}}}, ["tasks"]),
    _tool("ask_user", "Запросить уточнение, без которого нельзя продолжить.",
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


@close_task_commands
def run_team(service, toolbox, goal, emit, stopped, *, capacity=None, parallelism=None, report=None, trace=None,
             scheduled_role=None, relationship=None, attachment_paths=(), mcp_servers=(),
             reasoning=None, usage_progress=None, previous_result="", strategy=None,
             initial_context="", context_document=""):
    if isinstance(service, AIService):
        service = copy(service)
        service._file_read_version = getattr(toolbox, "read_version", None)
        service.last_usage = {}
        service.last_stop_reason = ""
    root = getattr(toolbox, "root", None)
    if context_document and root and not stopped():
        try:
            path = Path(context_document).resolve()
            relative = path.relative_to(Path(root).resolve()).as_posix()
            emit(0, "Руководитель", goal, "Читает открытый документ")
            limit = 2 if path.suffix.lower() == ".pdf" else 40
            page = json.loads(toolbox.read_document(relative, limit=limit))
            text = page.get("text")
            if text is None:
                text = "\n\n".join(f"[Страница {item['page']}]\n{item['text']}" for item in page["items"])
            initial_context = (
                f"Открытый документ проекта {relative}. Для продолжения используй read_document.\n"
                + clip_utf8(text, 6000)
            )
            if page.get("next_offset") is not None:
                initial_context += f"\nСледующий диапазон начинается с offset={page['next_offset']}."
        except (OSError, ValueError, RuntimeError) as exc:
            if trace and not stopped():
                trace(0, f"Контекст открытого документа недоступен. {exc}")
    selected = strategy or getattr(service, 'config', {}).get('ai_team_strategy', 'single')
    if selected == 'three_stage' and not mcp_servers and not scheduled_role:
        from services.three_stage_team import run_three_stage
        return run_three_stage(service, toolbox, goal, emit, stopped, report=report,
                               trace=trace, reasoning=reasoning, usage_progress=usage_progress,
                               previous_result=previous_result, attachment_paths=attachment_paths,
                               relationship=relationship, initial_context=initial_context)
    if selected == 'three_stage' and trace:
        trace(0, 'Используется динамический режим: внешние MCP или запланированная роль пока требуют прежнего маршрута.')
    started = time.monotonic()
    learning_warning = ""
    optimizer = None
    if _optimizer_enabled(service):
        try:
            optimizer = TeamOptimizer(
                getattr(toolbox, "team_cache", None), str(getattr(toolbox, "root", "")),
                str(getattr(service, "base_url", "")) + "|" + str(getattr(service, "model", "")))
        except Exception as exc:
            learning_warning = f"История оптимизации недоступна: {exc}"
            optimizer = TeamOptimizer()
    team_description = {}
    optimizer_tools = []
    if optimizer is not None:
        optimizer_tools = [
            _tool("optimize_team", "Рассчитать рекомендуемое число исполнителей по сложности и истории. "
                  "Вызывай после оценки задачи, до назначения команды; work_units — независимые части работы.",
                  {key: {"type": "number"} for key in (
                      "work_units", "parallel_fraction", "risk", "quality_weight", "time_weight", "cost_weight")},
                  ["work_units", "parallel_fraction", "risk"]),
            _tool("assess_team", "При необходимости сохрани оценку состава команды. "
                  "Отделяй фактические проверки от предположений.",
                  {key: {"type": "string"} for key in ("description", "evidence", "inefficiencies", "next_structure")},
                  ["description", "evidence", "inefficiencies", "next_structure"]),
        ]
    previous_tool = _tool("read_previous_result", "Прочитать предыдущую задачу и итог, только если текущий вопрос ссылается на них.",
                          {"offset": {"type": "integer", "minimum": 0},
                           "length": {"type": "integer", "minimum": 1, "maximum": 12000}}, [])
    definitions = toolbox.definitions()
    from services.ide_mcp import IDEMCP
    ide = IDEMCP(toolbox)
    supervisor_tools = list(definitions)
    supervisor_names = {definition["function"]["name"] for definition in supervisor_tools}
    mcp_clients = {}
    mcp_tool_names = set()
    mcp_resource_names = set()
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
    completed = {}
    published = set()
    mutation_lock = RLock()
    evidence = TaskEvidence(goal, getattr(toolbox, "root", None))
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
                        if target.is_file() and not toolbox._outside_project(target):
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
                            evidence.observe(name, {**payload, "path": path}, message)
                            if trace:
                                trace(0, "Применено в фоне, снимок сохранён:\n" + edit.diff())
                            return message
                    except Exception as exc:
                        return f"Ошибка правки: {exc}"
                value = ide.call(name, payload)
                evidence.observe(name, payload, value)
                return value
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
            if reasoning:
                options["on_reasoning"] = lambda piece: reasoning(agent_id, piece)
            if usage_progress:
                options["on_round_usage"] = usage_progress
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
        nonlocal assigned, question, failed_agents, requirements_ready
        if stopped() or question:
            return "Запуск остановлен."
        if name == "read_previous_result" and previous_result:
            offset, length = args.get("offset", 0), args.get("length", 6000)
            if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 12000:
                return "Укажи offset >= 0 и length от 1 до 12000."
            excerpt = previous_result[offset:offset + length]
            if offset + length < len(previous_result):
                excerpt += f"\n[Продолжение доступно с offset={offset + length}.]"
            return excerpt
        if name in mcp_resource_names or name in mcp_tool_names:
            from services.mcp_client import MCPError, run_resource_tool, run_tool

            try:
                if name in mcp_resource_names:
                    return run_resource_tool(mcp_clients, name, args)
                return run_tool(mcp_clients, name, args)
            except MCPError as exc:
                return f"Ошибка MCP: {exc}"
        if name == "optimize_team":
            if optimizer is None:
                return "Оптимизация команды отключена (ai_team_optimizer)."
            try:
                recommendation = optimizer.recommend(**args)
                return json.dumps({key: recommendation[key] for key in
                                   ("recommendation", "total_with_supervisor", "history_samples")},
                                  ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                return f"Ошибка параметров оптимизации: {exc}"
        if name == "assess_team":
            if optimizer is None:
                return "Оптимизация команды отключена (ai_team_optimizer)."
            keys = ("description", "evidence", "inefficiencies", "next_structure")
            if any(not isinstance(args.get(key), str) or not args[key].strip() for key in keys):
                return "Заполни описание, доказательства, недостатки и предложение следующего состава."
            optimizer.assessment = {key: args[key] for key in keys}
            return "Оценка будет сохранена вместе с фактическими метриками запуска."
        if name in {"propose_edit", "reconcile_edit", "accept_proposal"} and edits_enabled:
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
        if assigned == 0 and scheduled_role and all(role != scheduled_role for role, _ in batch):
            return f"В первом пакете назначь запланированного агента с ролью {scheduled_role}."
        if len(set(batch)) != len(batch) or seen.intersection(batch):
            return "Уберите повторные задания или уточните цель повторной проверки."
        try:
            batch_results = scheduler.batch(tasks)
        except ValueError as exc:
            return f"Ошибка заданий: {exc}"
        seen.update(batch)
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

    current_status = "Координирует"

    def update_status(status):
        nonlocal current_status
        if status != current_status and not stopped():
            current_status = status
            emit(0, "Руководитель", goal, status)

    def traced_route(name, args):
        if isinstance(service, AIService):
            update_status("Выполняет инструменты")
        if trace:
            trace(0, name + "\n" + clip_utf8(json.dumps(args, ensure_ascii=False, indent=2), 2000))
        value = route(name, args)
        if trace:
            trace(0, "Снимок экрана передан модели." if isinstance(value, dict) and "__image__" in value else clip_utf8(str(value), 6000))
        return value

    def trace_tool_outcome(run):
        if trace and run.repeated:
            trace(0, f"Повторный вызов пропущен · {run.name}")
        elif trace and run.name in {"lookup_tools", "call_tool", "read_local_evidence"}:
            status = "получен результат" if run.ok else "вызов отклонён"
            trace(0, f"{run.name} · {status}")

    emit(0, "Руководитель", goal, "Координирует")
    prompt = (
        "Выполни задачу напрямую; делегируй независимые части только при необходимости. "
        "Для зависимых задач используй key/depends_on. Проверяй выводы и пересечения правок. "
        "Готовые предложения принимай по ID; пересечения согласуй через reconcile_edit. "
        "Если без уточнения продолжить нельзя, вызови ask_user. Проверяй именно запрошенное поведение. "
        "Для визуальной правки проверь отрисовку компонента. Сообщай только реально сделанное "
        "и результаты проверок после последней правки. Для явного запуска во встроенном терминале "
        "используй terminal_execute; для долгого процесса укажи wait_seconds=0. "
        "Процесс, запущенный в IDE, останавливай через terminal_control в его вкладке."
        "\n\nЗадача:\n" + goal
    )
    if scheduled_role:
        prompt += f"\nНазначь исполнителя с запланированной ролью {scheduled_role}."
    options = {"attachment_paths": attachment_paths} if attachment_paths else {}
    if initial_context:
        options["context"] = initial_context
    external_tools = []
    error = ""
    try:
        if mcp_servers:
            from services.mcp_client import MCPClient, resource_tools

            for config in mcp_servers:
                if stopped():
                    break
                client = MCPClient(config, timeout=15)
                mcp_clients[config.name] = client
                try:
                    client.connect()
                except Exception as exc:
                    del mcp_clients[config.name]
                    client.close()
                    if trace:
                        trace(0, f"MCP · {config.label}: {exc}")
                    continue
                for tool in client.tools:
                    if tool.full_name not in supervisor_names and tool.full_name not in mcp_tool_names:
                        mcp_tool_names.add(tool.full_name)
                        external_tools.append(tool.as_openai_tool())
            resources = resource_tools(mcp_clients)
            mcp_resource_names.update(tool["function"]["name"] for tool in resources)
            external_tools.extend(resources)
        live_options = ({"on_reasoning": (lambda piece: reasoning(0, piece)) if reasoning else None,
                         "on_usage": usage_progress,
                         "on_tool_end": trace_tool_outcome if trace else None,
                         "on_request": lambda: update_status("Ожидает ответ модели")}
                        if isinstance(service, AIService) else {})
        answer = service.converse(
            prompt, tools=TOOLS + optimizer_tools + supervisor_tools + external_tools
            + ([previous_tool] if previous_result else [])
            + ([EDIT_SCHEMA, accept, reconcile] if edits_enabled else []),
            tool_runner=traced_route, should_stop=lambda: stopped() or bool(question),
            unlimited_rounds=True, **options, **live_options,
        )
        usage.add(service.last_usage)
    except Exception as exc:
        answer, error = "", str(exc)
        if isinstance(service, AIService):
            usage.add(service.last_usage)
    finally:
        for client in mcp_clients.values():
            client.close()
    edits = candidate.files() if not question and not stopped() else []
    reason = getattr(service, "last_stop_reason", None)
    missing_evidence = evidence.blocking_reason(answer, edits)
    incomplete = not question and not stopped() and (
        bool(error) or reason in {"empty", "stalled"} or not answer.strip() or (scheduled_role and not assigned)
        or failed_agents > 0 or missing_evidence
    )
    if incomplete:
        detail = error or {
            "empty": "Модель руководителя вернула пустой ответ.",
            "stalled": STALLED_NOTICE,
        }.get(reason, missing_evidence or ("Есть незавершённые задания агентов." if failed_agents else
              "Руководитель не завершил работу команды."))
        lines = [f"**Частичный итог.** {detail}", f"Файлов в подготовленных правках: {len(edits)}."]
        unconfirmed_answer = ((evidence.change_requested and not evidence.revision)
                              or (evidence.terminal_requested and not evidence.terminal_launch)
                              or evidence.unconfirmed_test_claim(answer))
        if answer.strip() and not error and not unconfirmed_answer and reason not in {"empty", "stalled"}:
            lines.append(answer)
        for number, result in sorted(completed.items()):
            title = ROLES[result["role"]][0]
            summary = result.get("error") or result.get("result", "")
            lines.append(f"- **{title} · {number}** — {' '.join(summary.split())[:280]}")
        answer = "\n\n".join(lines)
    facts = evidence.report()
    if facts and not question and not stopped():
        answer = "\n\n".join((answer, facts))
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
    experience = {}
    if optimizer is not None:
        try:
            experience = optimizer.record(goal=goal, agents=agents, seconds=time.monotonic() - started,
                                          usage=usage.as_dict(), outcome=outcome)
        except Exception as exc:
            experience = {"plan": optimizer.plan}
            learning_warning = f"Не удалось сохранить опыт команды: {exc}"
        if assigned and not optimizer.assessment:
            experience["assessment_missing"] = True
    if learning_warning:
        experience["warning"] = learning_warning
        answer += "\n\n" + learning_warning
    if trace and experience:
        trace(0, "Оптимизация команды:\n" + json.dumps(experience, ensure_ascii=False, indent=2))
    return TeamResult(answer, question, usage.as_dict(), failed_agents, edits,
                      not (incomplete or question or stopped()), sorted(evidence.changed_files), experience)
