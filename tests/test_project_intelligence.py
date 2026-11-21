import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.project_intelligence import ProjectIntelligenceEngine
from core.analyzers.registry import analyzer_map



def test_all_analyzers_are_registered():
    specs = analyzer_map()
    assert {"structure", "dependencies", "metrics", "health", "security", "binaries"} <= set(specs)


def test_full_analysis_does_not_start_the_frozen_app(tmp_path, monkeypatch):
    import subprocess

    (tmp_path / "example.py").write_text("assert True\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "CodeLane.exe")

    def no_process(*args, **kwargs):
        raise AssertionError("Project analysis must not spawn the application")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    result = ProjectIntelligenceEngine(str(tmp_path), enabled_analyzers=analyzer_map()).analyze()
    assert result.backend_status["bandit"] == "available + executed"
    assert any(issue.code == "B101" for issue in result.issues)
    assert result.duration_ms > 0


def test_empty_analyzer_selection_is_a_true_noop(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('should not be scanned')\n", encoding="utf-8")
    engine = ProjectIntelligenceEngine(str(tmp_path), enabled_analyzers=[])
    result = engine.analyze()
    assert result.files == {}
    assert result.issues == []


def test_python_structure_analysis(tmp_path: Path):
    source = tmp_path / "main.py"
    source.write_text("class A:\n    def run(self):\n        return 1\n", encoding="utf-8")
    engine = ProjectIntelligenceEngine(str(tmp_path), enabled_analyzers={"structure"})
    result = engine.analyze()
    assert str(source) in result.files
    assert any(symbol.name == "A" for symbol in result.symbols)
    assert any(symbol.name == "run" for symbol in result.symbols)


if __name__ == "__main__":
    test_all_analyzers_are_registered()
    print("test_all_analyzers_are_registered: PASS")
    with tempfile.TemporaryDirectory() as td:
        test_empty_analyzer_selection_is_a_true_noop(Path(td))
        print("test_empty_analyzer_selection_is_a_true_noop: PASS")
    with tempfile.TemporaryDirectory() as td:
        test_python_structure_analysis(Path(td))
        print("test_python_structure_analysis: PASS")
    print("ALL PROJECT INTELLIGENCE TESTS PASSED!")

