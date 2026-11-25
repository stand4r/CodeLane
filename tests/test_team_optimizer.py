import pytest

from services.team_cache import TeamCache
from services.team_optimizer import TeamOptimizer


def test_cost_and_serial_work_favor_small_team():
    optimizer = TeamOptimizer()
    cost = optimizer.recommend(10, 0.9, 0.5, 0, 0, 1)
    speed = optimizer.recommend(10, 0.9, 0.5, 0, 1, 0)
    serial = optimizer.recommend(10, 0, 0.5, 0, 1, 0)
    assert cost["recommendation"]["workers"] == serial["recommendation"]["workers"] == 1
    assert speed["recommendation"]["workers"] > 1


@pytest.mark.parametrize("args", [{"risk": float("nan")}, {"work_units": -1},
                                 {"time_weight": -1}, {"work_units": True},
                                 {"quality_weight": 0, "time_weight": 0, "cost_weight": 0}])
def test_invalid_input(args):
    with pytest.raises(ValueError):
        TeamOptimizer().recommend(**args)


def test_experience_persists_and_changes_prediction(tmp_path):
    cache = TeamCache(tmp_path)
    optimizer = TeamOptimizer(cache, "project", "model")
    before = optimizer.recommend(3)["recommendation"]
    optimizer.assessment = {"next_structure": "Меньше повторных ревью"}
    for _ in range(8):
        optimizer.record(goal="Fix", agents=[{"role": "code"}] * before["workers"],
                         seconds=50, usage={"total_tokens": 5000}, outcome="failed")
    fresh = TeamOptimizer(cache, "project", "model")
    after = fresh.recommend(3)
    assert after["history_samples"] == 8
    assert after["recommendation"]["workers"] != before["workers"]
    assert after["recent_lessons"][0]["assessment"] == optimizer.assessment
    assert TeamOptimizer(cache, "other", "model").history() == []
    assert TeamOptimizer(cache, "project", "other").history() == []


def test_cancelled_and_question_runs_are_not_training_failures(tmp_path):
    optimizer = TeamOptimizer(TeamCache(tmp_path), "p", "m")
    before = optimizer.recommend()
    for outcome in ("cancelled", "question"):
        optimizer.record(goal="Q", agents=[], seconds=1, usage={}, outcome=outcome)
    assert optimizer.recommend() == before
    assert len(optimizer.history()) == 2


def test_team_records_nested_structure_and_supervisor_assessment(monkeypatch, tmp_path):
    import json
    from services import agent_team
    from services.agent_tools import LocalToolbox

    def delegate(*args, **kwargs):
        return json.dumps({"role": args[3]["role"], "result": "ok"})
    monkeypatch.setattr(agent_team, "delegate", delegate)

    class Service:
        last_usage = {"total_tokens": 123}
        model = "test"
        config = {"ai_team_optimizer": True}
        def converse(self, prompt, *, tool_runner, **kwargs):
            tool_runner("optimize_team", {"work_units": 2, "parallel_fraction": 0.5, "risk": 0.2})
            tool_runner("assign_team", {"tasks": [{"role": "requirements", "task": "Criteria"}]})
            tool_runner("assess_team", dict(description="One worker", evidence="No tests",
                                           inefficiencies="None observed", next_structure="Add tester for edits"))
            return "Done"

    toolbox = LocalToolbox(str(tmp_path))
    toolbox.team_cache = TeamCache(tmp_path / "cache")
    result = agent_team.run_team(Service(), toolbox, "Inspect", lambda *a: None, lambda: False)
    history = TeamOptimizer(toolbox.team_cache, str(toolbox.root), "|test").history()
    assert len(history) == 1
    assert history[0]["agents"][0]["task"] == "Criteria"
    assert history[0]["tokens"] == 123
    assert not history[0]["assessment_missing"]
    assert result.optimization["outcome"] == "complete"


def test_optimizer_is_off_unless_enabled(tmp_path):
    from services import agent_team
    from services.agent_tools import LocalToolbox

    class Service:
        last_usage = {}
        def converse(self, prompt, *, tools, tool_runner, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            assert "optimize_team" not in names and "assess_team" not in names
            assert tool_runner("optimize_team", {"work_units": 2}) == \
                "Оптимизация команды отключена (ai_team_optimizer)."
            assert tool_runner("assess_team", dict(
                description="d", evidence="e", inefficiencies="i", next_structure="n")) == \
                "Оптимизация команды отключена (ai_team_optimizer)."
            return "Готово"

    toolbox = LocalToolbox(str(tmp_path))
    toolbox.team_cache = TeamCache(tmp_path / "cache")
    traces = []
    result = agent_team.run_team(Service(), toolbox, "Inspect", lambda *a: None, lambda: False,
                                 trace=lambda number, text: traces.append(text))
    assert result.complete
    assert result.optimization == {}
    assert not any(text.startswith("Оптимизация команды:\n") for text in traces)
    assert TeamOptimizer(toolbox.team_cache, str(toolbox.root), "|").history() == []
