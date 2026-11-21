from threading import Barrier, Event

import pytest

from services.team_scheduler import TeamScheduler


def test_dependencies_wait_for_results_and_independent_tasks_overlap():
    overlap = Barrier(2)
    order = []

    def run(number, role, task, children, context):
        if role in {"code", "design"}:
            overlap.wait(timeout=2)
        if role == "test":
            assert "code" in order and "design" in order
            assert "Результаты зависимостей" in context
            assert "code output" in context
        order.append(role)
        return {"role": role, "result": role + " output"}, {}

    scheduler = TeamScheduler(run, lambda *_: None, Event().is_set, 12, 2)
    result = scheduler.batch([
        {"key": "tests", "role": "test", "task": "check", "depends_on": ["code", "design"]},
        {"key": "code", "role": "code", "task": "code"},
        {"key": "design", "role": "design", "task": "design"},
    ])
    assert order[-1] == "test"
    assert result[0]["agent_id"] == 1


def test_nested_delegation_with_single_slot_finishes_before_parent():
    events = []
    links = []

    def run(number, role, task, children, context):
        if task == "parent":
            result = children([{"role": "test", "task": "child"}])
            assert result[0]["result"] == "child"
            assert events == ["child"]
        events.append(task)
        return {"role": role, "result": task}, {}

    scheduler = TeamScheduler(run, lambda *_: None, Event().is_set, 12, 1,
                              lambda *link: links.append(link))
    scheduler.batch([{"role": "code", "task": "parent"}])
    assert events == ["child", "parent"]
    assert links == [(1, 0, []), (2, 1, [])]
    assert scheduler.results[2][0]["parent_id"] == 1


def test_dependency_cycles_and_unknown_keys_are_rejected_before_execution():
    scheduler = TeamScheduler(lambda *_: pytest.fail("Must not start"), lambda *_: None,
                              Event().is_set, 12, 2)
    for dependencies in (["missing"], ["a"]):
        with pytest.raises(ValueError, match="цикл"):
            scheduler.batch([{"key": "a", "role": "code", "task": "task", "depends_on": dependencies}])
    assert scheduler.assigned == 0


def test_failed_child_marks_parent_partial_and_blocks_dependents():
    states = []

    def run(number, role, task, children, context):
        if task == "parent":
            children([{"role": "test", "task": "fail"}])
            return {"role": role, "result": "parent output"}, {}
        if task == "fail":
            return {"role": role, "error": "Исчерпан лимит раундов"}, {}
        pytest.fail("Dependent should not run")

    scheduler = TeamScheduler(run, lambda *state: states.append(state), Event().is_set, 12, 2)
    result = scheduler.batch([
        {"key": "parent", "role": "code", "task": "parent"},
        {"key": "dependent", "role": "test", "task": "dependent", "depends_on": ["parent"]},
    ])
    assert result[0]["child_errors"]
    assert "error" in result[1]
    assert any(state[-1] == "Частичный итог" for state in states)


def test_nested_assignments_share_capacity_and_cancellation():
    cancel = Event()

    def run(number, role, task, children, context):
        if task == "parent":
            with pytest.raises(ValueError, match="общий лимит"):
                children([{"role": "test", "task": "too many"},
                          {"role": "test", "task": "also too many"}])
            cancel.set()
            result = children([{"role": "test", "task": "cancelled"}])
            assert "Отменено" in result[0]["error"]
            return {"role": role, "result": "stopped"}, {}
        pytest.fail("Cancelled agent should never call the provider")

    scheduler = TeamScheduler(run, lambda *_: None, cancel.is_set, 2, 1)
    scheduler.batch([{"role": "code", "task": "parent"}])
    assert scheduler.assigned == 2
