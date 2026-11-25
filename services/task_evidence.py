"""Local evidence for actions and test claims in a team task."""

import json
import os
import re

from services.ai_budget import clip_utf8


_CHANGE = re.compile(
    r"(?:^|[.!?\n]\s*|\b(?:и|также|затем)\s+)(?:пожалуйста,?\s+)?"
    r"(?:исправь|доработай|добавь|измени|изменить|обнови|создай|удали|"
    r"почини|убери|ускорь|внедри|реализуй|переименуй|fix|edit|change|implement|add|remove"
    r"|(?:сделай|напиши)\s+[^\n.!?]{0,40}\b(?:лабу|лабораторн\w*|проект|программу|приложение|файл))\b",
    re.IGNORECASE,
)
_TEST_COMMAND = re.compile(
    r"\b(?:pytest|unittest|(?:(?:npm|pnpm|yarn|go|cargo|dotnet)\s+test))\b", re.IGNORECASE
)
_TEST_REQUEST = re.compile(
    r"\b(?:протестируй|запусти\s+тест\w*|прогони\s+тест\w*|run\s+(?:the\s+)?tests)\b",
    re.IGNORECASE,
)
_TEST_PASS = re.compile(
    r"(?:\b(?:тест\w*|pytest|tests?)\b[^\n]{0,120}(?<!не )(?<!not )\b"
    r"(?:прошл\w*|пройден\w*|passed|успешно)\b"
    r"|\b\d+\s+(?:тест\w*\s+прошл\w*|passed)\b)",
    re.IGNORECASE,
)
_TEST_RUN = re.compile(
    r"\b(?:запустил\w*|прогнал\w*|ran)\s+(?:the\s+)?(?:тест\w*|pytest|tests?)\b", re.IGNORECASE
)
_TERMINAL_REQUEST = re.compile(
    r"\b(?:в|во)\s+(?:встроенном\s+)?терминале\b|\b(?:in|inside)\s+(?:the\s+)?integrated terminal\b",
    re.IGNORECASE,
)
_LAUNCH = re.compile(r"\b(?:запусти|запуски|запускай|выполни|run|launch|start)\b", re.IGNORECASE)
_DISK_EDIT_RESULTS = ("Создан файл", "Обновлён файл", "Изменён файл")
_BUFFER_EDIT_RESULTS = ("Правка применена во вкладке", "Открытая вкладка")


class TaskEvidence:
    def __init__(self, goal, root=None):
        self.change_requested = bool(_CHANGE.search(goal))
        self.tests_requested = bool(_TEST_REQUEST.search(goal))
        self.terminal_requested = bool(_LAUNCH.search(goal) and _TERMINAL_REQUEST.search(goal))
        self.root = root or "."
        self.changed_files = set()
        self.unsaved = set()
        self.revision = 0
        self.tests = {}
        self.command_revisions = {}
        self.terminal_launch = ""

    def _path_key(self, path):
        return os.path.normcase(os.path.abspath(os.path.join(self.root, path)))

    def observe(self, name, args, value):
        if not isinstance(value, str):
            return
        if name == "apply_patch":
            for change in self._object(value).get("changes", []):
                self.changed_files.add(change["path"])
                if change.get("unsaved"):
                    self.unsaved.add(self._path_key(change["path"]))
                self.revision += 1
        elif name in {"edit_file", "write_file"} and value.startswith(_DISK_EDIT_RESULTS + _BUFFER_EDIT_RESULTS):
            if name == "edit_file" and args.get("old_text") == args.get("new_text"):
                return
            self.changed_files.add(args["path"])
            if value.startswith(_BUFFER_EDIT_RESULTS):
                self.unsaved.add(self._path_key(args["path"]))
            self.revision += 1
        elif name in {"editor_save", "todo_edit", "explorer_edit", "project_config_edit", "git_change"}:
            if self._object(value):
                if name == "editor_save":
                    self.unsaved.discard(self._path_key(args["path"]))
                self.revision += 1
        elif name in {"run_project_tests", "run_terminal_command", "terminal_execute", "exec_command", "write_stdin"}:
            result = self._object(value) if name in {"terminal_execute", "exec_command", "write_stdin"} else {}
            command = str(args.get("command") or args.get("cmd") or result.get("command", ""))
            if result.get("started") and (not result.get("completed") or result.get("exit_code") == 0):
                self.terminal_launch = command
            if name != "run_project_tests" and not _TEST_COMMAND.search(command):
                return
            if result:
                code = result.get("exit_code") if result.get("completed") else None
            else:
                exit_code = re.match(r"Код возврата: (-?\d+)(?:\s|$)", value)
                code = int(exit_code[1]) if exit_code else None
            label = command or f"Тесты проекта · {args.get('target') or 'весь проект'}"
            cwd = args.get("cwd") or result.get("cwd")
            if cwd:
                label = f"{cwd} · {label}"
            self.tests.pop(label, None)
            failure = value if code is None and value.startswith("Ошибка") else ""
            revision, unsaved = self.revision, bool(self.unsaved)
            if name in {"exec_command", "write_stdin"} and result.get("session_id"):
                session = result["session_id"]
                revision, unsaved = self.command_revisions.setdefault(session, (revision, unsaved))
                if result.get("completed") and not result.get("has_more_output"):
                    self.command_revisions.pop(session, None)
            self.tests[label] = (revision, code, unsaved, failure)

    @staticmethod
    def _object(value):
        try:
            data = json.loads(value)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def blocking_reason(self, answer, pending_edits):
        if self.terminal_requested and not self.terminal_launch:
            return "Запуск во встроенном терминале не подтверждён."
        if self.change_requested and not self.revision:
            return ("Правки подготовлены и ожидают применения." if pending_edits
                    else "Изменения для этой задачи инструментами не подтверждены.")
        current_tests = self._current_tests()
        if self.tests_requested and (not current_tests or any(code != 0 for code in current_tests)):
            return "Нет успешного результата запрошенных тестов для текущего состояния."
        if self.unconfirmed_test_claim(answer):
            return "Заявленный результат тестов не подтверждён инструментами для текущего состояния."
        if self.change_requested and self.unsaved:
            return "Часть правок ещё не сохранена на диск."
        return ""

    def _current_tests(self):
        return [code for revision, code, unsaved, _ in self.tests.values()
                if revision == self.revision and not unsaved]

    def unconfirmed_test_claim(self, answer):
        return ((_TEST_PASS.search(answer) and 0 not in self._current_tests())
                or (_TEST_RUN.search(answer) and not any(code is not None for _, code, _, _ in self.tests.values())))

    def report(self):
        if not self.revision and not self.tests and not self.terminal_launch:
            return ""
        lines = []
        if self.changed_files:
            paths = ", ".join(f"`{clip_utf8(path, 200)}`" for path in sorted(self.changed_files)[:12])
            lines.append(f"Изменено: {paths}.")
        if self.unsaved:
            lines.append("Есть правки, оставшиеся в буфере редактора.")
        if self.terminal_launch:
            lines.append(f"Подтверждён запуск во встроенном терминале · {clip_utf8(self.terminal_launch, 240)}")
        if self.tests:
            lines.append("Прогон тестов")
            for label, (revision, code, unsaved, failure) in list(self.tests.items())[-6:]:
                status = ("успешно" if code == 0 else "ошибка") if type(code) is int else "без подтверждённого кода выхода"
                if failure:
                    status = "запуск не выполнен · " + clip_utf8(failure.splitlines()[0], 250)
                if revision != self.revision:
                    status += ", до последних изменений"
                if unsaved:
                    status += ", при несохранённых правках"
                lines.append(f"- {clip_utf8(' '.join(label.split()), 240)} · {status}")
        else:
            lines.append("Запуск тестов не зафиксирован.")
        return "\n".join(lines)
