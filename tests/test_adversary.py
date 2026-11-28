"""Tests for the adversarial tester («Адвокат дьявола»).

Run: python tests/test_adversary.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adversary import (
    AdversaryReport,
    AdversaryRunner,
    Finding,
    discover_functions,
    failing_cases_pytest,
    generate_cases,
)

DEMO_SOURCE = '''
def head(items):
    return items[0]


def average(values):
    return sum(values) / len(values)


def label(value: str):
    return f"<{value}>"


def scale(factor):
    return 10 / factor
'''


def _write_demo(directory: Path, source: str = DEMO_SOURCE) -> str:
    path = directory / "demo_target.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


# ----------------------------------------------------------------------
# Discovery and input generation
# ----------------------------------------------------------------------
def test_discovery_finds_functions_and_signatures():
    specs = discover_functions(DEMO_SOURCE)
    names = [s.name for s in specs]
    assert names == ["head", "average", "label", "scale"]
    head = specs[0]
    assert head.arg_names == ["items"]
    assert head.lineno == 2
    assert "self" not in specs[0].arg_names


def test_discovery_skips_methods_and_handles_syntax_error():
    source = "class A:\n    def run(self):\n        return 1\n"
    assert discover_functions(source) == []
    assert discover_functions("def broken(:\n") == []


def test_roles_are_inferred_from_the_body():
    """Without annotations a parameter must not be fed ``None`` blindly."""
    source = '''
def by_index(items):
    return items[0]


def ratio(factor):
    return 10 / factor


def words(text):
    return text.split()


def keys(mapping):
    return mapping.items()
'''
    specs = {s.name: s for s in discover_functions(source)}
    assert specs["by_index"].roles.get("items") == "sequence"
    assert specs["ratio"].roles.get("factor") == "number"
    assert specs["words"].roles.get("text") == "string"
    assert specs["keys"].roles.get("mapping") == "mapping"


def test_generated_cases_are_relevant_and_bounded():
    specs = {s.name: s for s in discover_functions(DEMO_SOURCE)}
    cases = generate_cases(specs["head"])
    assert cases, "ожидался хотя бы один случай"
    assert len(cases) <= 24
    # The empty list must be tried: it is the classic boundary.
    assert any(case == [[]] for case in cases), cases
    # A sequence parameter must not receive an arbitrary number.
    assert not any(isinstance(case[0], int) for case in cases), cases

    annotated = generate_cases(specs["label"])
    assert all(isinstance(case[0], str) for case in annotated), annotated


def test_zero_argument_function_gets_one_empty_call():
    specs = discover_functions("def ping():\n    return 1\n")
    assert generate_cases(specs[0]) == [[]]


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------
def test_execute_reports_crash_with_line_number():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td))
        runner = AdversaryRunner(timeout=8)
        try:
            finding = runner.execute(path, "head", [[]])
        finally:
            runner.dispose()
    assert finding.kind == "error"
    assert finding.error_type == "IndexError"
    assert finding.line == 3          # return items[0]
    assert finding.failed is True


def test_execute_marks_clean_call_as_ok():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td))
        runner = AdversaryRunner(timeout=8)
        try:
            finding = runner.execute(path, "label", ["abc"])
        finally:
            runner.dispose()
    assert finding.kind == "ok"
    assert finding.failed is False
    assert finding.summary == "без ошибки"


def test_hang_is_detected_by_timeout():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td), "def spin():\n    while True:\n        pass\n")
        runner = AdversaryRunner(timeout=2)
        try:
            finding = runner.execute(path, "spin", [])
        finally:
            runner.dispose()
    assert finding.kind == "hang"
    assert "зависание" in finding.summary


def test_import_error_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td), "import module_that_does_not_exist\n")
        runner = AdversaryRunner(timeout=8)
        try:
            report = runner.run_file(path)
        finally:
            runner.dispose()
    # No top-level function survived the import error, so the run reports it
    # either as a missing-function finding or as an empty result.
    assert isinstance(report, AdversaryReport)


# ----------------------------------------------------------------------
# Minimization
# ----------------------------------------------------------------------
def test_minimization_shrinks_the_input():
    # Fails on any non-empty list, so a long input can be shrunk to one element.
    source = '''
def explode(values):
    if values:
        raise ValueError("boom")
    return 0
'''
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td), source)
        runner = AdversaryRunner(timeout=8)
        try:
            finding = Finding(function="explode",
                              args=[[1, 2, 3, 4, 5, 6, 7, 8]], kind="error",
                              error_type="ValueError", error_message="boom")
            minimized = runner.minimize(path, finding)
        finally:
            runner.dispose()
    assert len(minimized.args[0]) < 8, minimized.args
    assert minimized.minimized is True


# ----------------------------------------------------------------------
# Whole-file run and report
# ----------------------------------------------------------------------
def test_transient_windows_startup_crash_is_retried(tmp_path, monkeypatch):
    if os.name != "nt":
        return
    from core import adversary

    path = _write_demo(tmp_path)
    attempts = []

    def run(command, **_kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(command, 0xC0000005, "", "")
        return subprocess.CompletedProcess(command, 0,
                                           adversary.RESULT_MARKER + json.dumps({"kind": "ok"}), "")

    monkeypatch.setattr(adversary, "run_hidden", run)
    runner = AdversaryRunner()
    try:
        assert runner.execute(path, "label", ["ok"]).kind == "ok"
        assert len(attempts) == 2
    finally:
        runner.dispose()


def test_run_file_finds_the_deliberate_bugs():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td))
        runner = AdversaryRunner(timeout=8)
        try:
            report = runner.run_file(path)
        finally:
            runner.dispose()
    assert report.functions_checked == 4
    assert report.cases_run > 0
    functions = {finding.function for finding in report.failures}
    assert {"head", "average", "scale"} <= functions
    # The clean function must not be reported.
    assert "label" not in functions
    groups = report.groups()
    assert groups, "группы находок не должны быть пустыми"


def test_report_markdown_and_pytest_export():
    with tempfile.TemporaryDirectory() as td:
        path = _write_demo(Path(td))
        runner = AdversaryRunner(timeout=8)
        try:
            report = runner.run_file(path)
        finally:
            runner.dispose()
    markdown = report.to_markdown()
    assert "Испытания" in markdown
    assert "head" in markdown
    pytest_text = failing_cases_pytest(path, report.failures)
    assert "parametrize" in pytest_text
    assert "'head'" in pytest_text
    assert "test_no_crash" in pytest_text


def test_report_survives_unreadable_file():
    runner = AdversaryRunner(timeout=5)
    try:
        report = runner.run_file(str(Path(tempfile.gettempdir()) / "no_such_file_xyz.py"))
    finally:
        runner.dispose()
    assert report.error
    assert report.findings == []


def test_dashboard_can_repeat_adversary_run_and_clear_old_report(tmp_path, monkeypatch, qtbot):
    from core.adversary import AdversaryReport
    from widgets.ProjectIntelligenceDashboard import ProjectIntelligenceDashboard

    panel = ProjectIntelligenceDashboard()
    qtbot.addWidget(panel)
    path = str(tmp_path / "original.py")
    monkeypatch.setattr(panel, "_current_editor_path", lambda: path)
    monkeypatch.setattr(AdversaryRunner, "run_file", lambda _, source: AdversaryReport(module_path=source))
    try:
        for _ in range(2):
            panel._run_adversary()
            panel.set_adversary_file(str(tmp_path / "other.py"))
            qtbot.waitUntil(lambda: panel._adversary_thread is None, timeout=3000)
            assert panel._adversary_report.module_path == path
            assert panel.adversary_export_btn.isEnabled()
        panel.set_adversary_file(None)
        assert panel._adversary_report is None
        assert not panel.adversary_export_btn.isEnabled()
    finally:
        panel.shutdown()


def test_analysis_sections_share_one_page_and_follow_selected_file(tmp_path, monkeypatch, qtbot):
    from core.project_intelligence import FileInfo, ProjectAnalysis, ProjectIntelligenceEngine
    from widgets.ProjectIntelligenceDashboard import ProjectIntelligenceDashboard

    selected = str(tmp_path / "selected.py")
    active = str(tmp_path / "active.py")
    panel = ProjectIntelligenceDashboard()
    qtbot.addWidget(panel)
    panel.resize(300, 480)
    panel.show()
    panel.engine = ProjectIntelligenceEngine(str(tmp_path))
    report = ProjectAnalysis(str(tmp_path), files={
        selected: FileInfo(selected, "selected", language="python"),
        active: FileInfo(active, "active", language="python"),
    })
    panel.engine.analysis = report
    panel._populate(report)
    monkeypatch.setattr(panel, "_current_editor_path", lambda: active)
    qtbot.waitUntil(lambda: panel.analysis_scroll.verticalScrollBar().maximum() > 0)

    assert all(panel._analysis_body.isAncestorOf(widget) for widget in
               (panel.tips_tree, panel.structure_tree, panel.impact_tree, panel.adversary_tree))
    assert panel.tips_tree.isVisible() and panel.adversary_tree.isVisible()
    panel.resize(1000, 580)
    qtbot.waitUntil(lambda: panel._wide_layout)
    tips = panel.section_grid.indexOf(panel._analysis_sections["tips"])
    structure = panel.section_grid.indexOf(panel._analysis_sections["structure"])
    assert panel.section_grid.getItemPosition(tips)[:2] == (0, 0)
    assert panel.section_grid.getItemPosition(structure)[:2] == (0, 1)
    panel.resize(300, 480)
    qtbot.waitUntil(lambda: not panel._wide_layout)
    panel.structure_tree.setCurrentItem(panel.structure_tree.topLevelItem(1))
    panel.structure_impact_button.click()
    assert panel.impact_combo.currentData() == selected
    assert panel.analysis_scroll.verticalScrollBar().value() > 0

    panel.structure_test_button.click()
    assert panel._sync_adversary_file() == selected
    panel.adversary_editor_btn.click()
    assert panel._adversary_path == active
    panel.shutdown()


if __name__ == "__main__":
    tests = [
        ("test_discovery_finds_functions_and_signatures",
         test_discovery_finds_functions_and_signatures),
        ("test_discovery_skips_methods_and_handles_syntax_error",
         test_discovery_skips_methods_and_handles_syntax_error),
        ("test_roles_are_inferred_from_the_body", test_roles_are_inferred_from_the_body),
        ("test_generated_cases_are_relevant_and_bounded",
         test_generated_cases_are_relevant_and_bounded),
        ("test_zero_argument_function_gets_one_empty_call",
         test_zero_argument_function_gets_one_empty_call),
        ("test_execute_reports_crash_with_line_number",
         test_execute_reports_crash_with_line_number),
        ("test_execute_marks_clean_call_as_ok", test_execute_marks_clean_call_as_ok),
        ("test_hang_is_detected_by_timeout", test_hang_is_detected_by_timeout),
        ("test_import_error_is_reported_not_raised", test_import_error_is_reported_not_raised),
        ("test_minimization_shrinks_the_input", test_minimization_shrinks_the_input),
        ("test_run_file_finds_the_deliberate_bugs", test_run_file_finds_the_deliberate_bugs),
        ("test_report_markdown_and_pytest_export",
         test_report_markdown_and_pytest_export),
        ("test_report_survives_unreadable_file", test_report_survives_unreadable_file),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
