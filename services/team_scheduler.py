"""Dependency-aware, bounded execution including nested agent assignments."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import BoundedSemaphore, Lock

from services.agent_delegation import ROLES


class TeamScheduler:
    def __init__(self, run, emit, stopped, capacity, parallelism, relationship=None):
        self.run = run
        self.emit = emit
        self.stopped = stopped
        self.capacity = capacity
        self.slots = BoundedSemaphore(parallelism) if parallelism else None
        self.lock = Lock()
        self.assigned = 0
        self.results = {}
        self.relationship = relationship

    def batch(self, tasks, parent=0, depth=0):
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("Нужен список заданий.")
        entries = {}
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ValueError("Некорректное задание.")
            role, goal = task.get("role"), task.get("task")
            key = task.get("key", str(index + 1))
            dependencies = task.get("depends_on", [])
            if (not isinstance(role, str) or role not in ROLES or not isinstance(goal, str)
                    or not goal.strip() or not isinstance(key, str) or not key
                    or key in entries or not isinstance(dependencies, list)
                    or any(not isinstance(dep, str) for dep in dependencies)):
                raise ValueError("Укажите уникальный key, допустимую роль, цель и depends_on.")
            entries[key] = (role, goal, dependencies)
        pending, resolved = set(entries), set()
        while pending:
            ready = {key for key in pending if set(entries[key][2]) <= resolved}
            if not ready:
                raise ValueError("Зависимости содержат цикл или неизвестный key.")
            pending -= ready
            resolved |= ready
        with self.lock:
            if self.capacity is not None and self.assigned + len(tasks) > self.capacity:
                raise ValueError("Исчерпан общий лимит агентов команды.")
            ids = {key: self.assigned + index + 1 for index, key in enumerate(entries)}
            self.assigned += len(tasks)
        for key, (role, goal, dependencies) in entries.items():
            self.emit(ids[key], ROLES[role][0], goal, "Ждёт зависимости" if dependencies else "В очереди")
            if self.relationship:
                self.relationship(ids[key], parent, [ids[dep] for dep in dependencies])
        outputs = {}

        def execute(key):
            role, goal, dependencies = entries[key]
            if any("error" in outputs[dep] or outputs[dep].get("child_errors") for dep in dependencies):
                self.emit(ids[key], ROLES[role][0], goal, "Заблокирован")
                return {"role": role, "error": "Зависимый агент завершился с ошибкой.",
                        "dependencies": [ids[d] for d in dependencies]}, {}
            while self.slots and not self.slots.acquire(timeout=0.1):
                if self.stopped():
                    return {"role": role, "error": "Отменено."}, {}
            try:
                if self.stopped():
                    return {"role": role, "error": "Отменено."}, {}

                def children(child_tasks):
                    self.emit(ids[key], ROLES[role][0], goal, "Ждёт субагентов")
                    if self.slots:
                        self.slots.release()
                    try:
                        return self.batch(child_tasks, ids[key], depth + 1)
                    finally:
                        if self.slots:
                            self.slots.acquire()
                        self.emit(ids[key], ROLES[role][0], goal, "Работает")

                context = "\n\nРезультаты зависимостей:\n" + str(
                    {dep: outputs[dep] for dep in dependencies}) if dependencies else ""
                result, tokens = self.run(ids[key], role, goal, children, context)
                with self.lock:
                    failures = [number for number, (child, _) in self.results.items()
                                if child.get("parent_id") == ids[key] and
                                ("error" in child or child.get("child_errors"))]
                if failures and result is not None:
                    result["child_errors"] = failures
                    self.emit(ids[key], ROLES[role][0], goal, "Частичный итог")
                return result, tokens
            finally:
                if self.slots:
                    self.slots.release()

        pending = set(entries)
        with ThreadPoolExecutor(max_workers=min(len(tasks), self.capacity) if self.capacity else len(tasks)) as pool:
            futures = {}
            while pending or futures:
                for key in list(entries):
                    if key in pending and all(dep in outputs for dep in entries[key][2]):
                        pending.remove(key)
                        futures[pool.submit(execute, key)] = key
                future = next(as_completed(futures))
                key = futures.pop(future)
                result, tokens = future.result()
                result = result or {"role": entries[key][0], "error": "Отменено."}
                result["agent_id"] = ids[key]
                result["parent_id"] = parent
                result["task"] = entries[key][1]
                result["dependencies"] = [ids[dep] for dep in entries[key][2]]
                outputs[key] = result
                with self.lock:
                    self.results[ids[key]] = (result, tokens)
        return [outputs[key] for key in entries]
