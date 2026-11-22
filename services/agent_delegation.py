"""Bounded, read-only subtasks for the workbench agent."""

from copy import copy
import json


ROLES = {
    "code": ("Код", {"list_directory", "search_in_project", "read_file", "lsp_query",
                    "analysis_details", "dependency_graph"}),
    "review": ("Ревью", {"search_in_project", "read_file", "git_read", "analysis_details", "lsp_query"}),
    "docs": ("Документация", {"list_directory", "search_in_project", "read_file", "lsp_query"}),
    "design": ("Дизайнер", {"list_directory", "search_in_project", "read_file"}),
    "test": ("Тестировщик", {"list_directory", "search_in_project", "read_file", "analysis_details", "test_results"}),
    "requirements": ("Постановка задачи", {"list_directory", "search_in_project", "read_file"}),
}

DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "Поручить отдельную исследовательскую подзадачу субагенту. Он вернёт выводы и пути к источникам.",
        "parameters": {
            "type": "object",
            "properties": {"role": {"type": "string", "enum": list(ROLES)},
                           "task": {"type": "string"}},
            "required": ["role", "task"],
        },
    },
}


def delegate(service, toolbox, definitions, args, should_stop, on_usage=None, *, collect_edits=False, trace=None,
             assign_children=None, dependency_context="", tool_runner=None, round_budget=None):
    from services.agent_edits import EDIT_SCHEMA, validate_fragment
    from services.ide_mcp import IDEMCP

    role, task = args.get("role"), args.get("task")
    if role not in ROLES or not isinstance(task, str) or not task.strip():
        raise ValueError("Укажи роль и конкретную подзадачу.")
    title, tool_names = ROLES[role]
    tools = list(definitions) if assign_children else [item for item in definitions if item["function"]["name"] in tool_names]
    if collect_edits:
        tools.append(EDIT_SCHEMA)
    if assign_children:
        from services.agent_team import TOOLS
        tools.append(TOOLS[0])
    allowed = {item["function"]["name"] for item in tools}
    ide = IDEMCP(toolbox)
    proposals = []

    def run_tool(name, payload):
        if should_stop():
            return "Ошибка: работа субагента остановлена."
        if name not in allowed:
            return "Ошибка: инструмент недоступен субагенту."
        payload = dict(payload or {})
        if name == "assign_team" and assign_children:
            try:
                return json.dumps(assign_children(payload.get("tasks")), ensure_ascii=False)
            except ValueError as exc:
                return f"Ошибка подзадач: {exc}"
        if name == "propose_edit":
            try:
                path, old, new = validate_fragment(payload)
            except ValueError as exc:
                return f"Ошибка: {exc}"
            proposals.append({"path": path, "old_text": old, "new_text": new})
            return "Предложение передано руководителю. Файл не менялся."
        try:
            return tool_runner(name, payload) if tool_runner else ide.call(name, payload)
        except Exception as exc:
            return f"Ошибка: {exc}"

    def traced_tool(name, payload):
        if trace:
            trace(name + "\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        value = run_tool(name, payload)
        if trace:
            trace(value)
        return value

    child = copy(service)
    answer = child.converse(
        f"Роль: {title}. Выполни подзадачу в текущем проекте. Верни выводы и пути "
        f"с номерами строк, если они известны. Укажи выполненные проверки и оставшиеся ограничения. "
        "Цитируй только фрагменты, необходимые для решения руководителя. "
        + ("Используй доступные инструменты для выполнения задачи. Согласовывай пересекающиеся изменения с руководителем."
           if assign_children else "Работай только на чтение.")
        + (" Создавай необходимых субагентов через assign_team. Задай key и depends_on для зависимых поручений. "
           "Не делегируй повторную проверку уже подтверждённых фактов и простую задачу, которую "
           "можешь закончить несколькими обращениями к IDE. "
           "Вызов дождётся завершения детей и вернёт их результаты. "
           "Учти результаты детей в своём выводе. Единственный руководитель команды остаётся главным."
           if assign_children else "")
        + f"\n\nПодзадача: {task}" + dependency_context,
        tools=tools, tool_runner=traced_tool, should_stop=should_stop,
        max_rounds=32,
        **({"round_budget": round_budget} if round_budget is not None else {}),
    )
    if on_usage is not None:
        on_usage(getattr(child, "last_usage", {}))
    if should_stop():
        raise RuntimeError("Запрос остановлен.")
    if trace and len(answer) > 6000:
        trace(answer)
    stop_reason = getattr(child, "last_stop_reason", None)
    if stop_reason == "limit":
        if proposals:
            return json.dumps({"role": role, "error": "Субагент исчерпал лимит раундов до итогового ответа.",
                               "proposed_edits": proposals}, ensure_ascii=False)
        raise RuntimeError("Субагент исчерпал лимит раундов до итогового ответа.")
    if stop_reason == "empty" or not answer.strip():
        if proposals:
            return json.dumps({"role": role, "error": "Субагент не вернул итоговый ответ.",
                               "proposed_edits": proposals}, ensure_ascii=False)
        raise RuntimeError("Субагент не вернул итоговый ответ.")
    result = {"role": role, "result": answer.strip()}
    if proposals:
        result["proposed_edits"] = proposals
    return json.dumps(result, ensure_ascii=False)
