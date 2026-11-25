import pytest

from services.agent_tools import LocalToolbox, ToolError


def test_related_symbols_share_context_without_duplicate_lines(tmp_path):
    (tmp_path / "source.py").write_text("before\nalpha = 1\nbetween\nbeta = 2\nafter\n")
    result = LocalToolbox(str(tmp_path)).search_in_project(queries=["alpha", "beta"])
    assert "source.py:2: alpha = 1" in result
    assert "source.py:4: beta = 2" in result
    assert result.count("between") == 1
    assert "before" in result and "after" in result


def test_exact_file_search_never_scans_project(tmp_path, monkeypatch):
    import core.ignore_rules
    (tmp_path / "target.py").write_text("wanted = 12")
    def forbidden(*args, **kwargs):
        raise AssertionError("exact file search must not scan the tree")
    monkeypatch.setattr(core.ignore_rules, "walk_files", forbidden)
    assert "wanted = 12" in LocalToolbox(str(tmp_path)).search_in_project("wanted", glob="target.py")


def test_search_limit_and_context_can_be_controlled(tmp_path):
    (tmp_path / "a.py").write_text("skip\n" + "needle\n" * 100)
    result = LocalToolbox(str(tmp_path)).search_in_project("needle", context_lines=0, max_results=3)
    assert result.count("needle") == 3
    assert "skip" not in result and "ограничен" in result


def test_search_cancels_and_reports_invalid_patterns(tmp_path):
    box = LocalToolbox(str(tmp_path), should_stop=lambda: True)
    (tmp_path / "a.py").write_text("abc")
    with pytest.raises(ToolError, match="остановлен"):
        box.search_in_project("abc", glob="a.py")
    with pytest.raises(ToolError, match="регулярное"):
        box.search_in_project("[", regex=True)
    with pytest.raises(ToolError, match="массивом"):
        box.search_in_project(queries="abc")


def test_exact_path_cannot_leave_project(tmp_path):
    with pytest.raises(ToolError, match="ограничен"):
        LocalToolbox(str(tmp_path)).search_in_project("anything", glob="../outside.py")


def test_regex_search_passes_a_timeout_and_reports_expensive_matches(tmp_path, monkeypatch):
    import regex

    (tmp_path / "source.py").write_text("value = 42\n")
    toolbox = LocalToolbox(str(tmp_path))
    assert "value = 42" in toolbox.search_in_project(r"value\s*=\s*\d+", regex=True)

    class SlowMatcher:
        def search(self, line, *, timeout, concurrent):
            assert timeout > 0 and concurrent
            raise TimeoutError

    monkeypatch.setattr(regex, "compile", lambda *_: SlowMatcher())
    with pytest.raises(ToolError, match="слишком долго"):
        toolbox.search_in_project("value", regex=True)


def test_regex_search_works_when_the_optional_engine_is_missing(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "regex", None)
    (tmp_path / "source.py").write_text("value = 42\n")
    toolbox = LocalToolbox(str(tmp_path))
    assert "source.py:1: value = 42" in toolbox.search_in_project(
        r"value\s*=\s*\d+", regex=True, glob="source.py",
    )
