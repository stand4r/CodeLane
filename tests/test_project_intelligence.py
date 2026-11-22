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


def test_source_cache_survives_restart_and_checks_content(tmp_path, monkeypatch):
    import os

    from core.analyzers.polyglot import PolyglotSourceAnalyzer

    source = tmp_path / "main.py"
    source.write_bytes(b"def first():\n    pass\n")
    calls = []
    original = PolyglotSourceAnalyzer.analyze

    def count(self, path, text):
        calls.append(path)
        return original(self, path, text)

    monkeypatch.setattr(PolyglotSourceAnalyzer, "analyze", count)
    first = ProjectIntelligenceEngine(str(tmp_path)).analyze()
    restarted = ProjectIntelligenceEngine(str(tmp_path))
    reused = restarted.analyze()
    assert reused.files == first.files
    assert reused.structure_nodes == first.structure_nodes
    assert calls == [source]

    timestamp = source.stat()
    source.write_bytes(b"def other():\n    pass\n")
    os.utime(source, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    assert any(symbol.name == "other" for symbol in restarted.analyze().symbols)
    assert calls == [source, source]

    cache_entry, = (tmp_path / ".codelane" / "analysis-cache" / "source").glob("*.json")
    cache_entry.write_text("{broken", encoding="utf-8")
    assert any(symbol.name == "other" for symbol in ProjectIntelligenceEngine(str(tmp_path)).analyze().symbols)
    assert calls == [source, source, source]


def test_binary_cache_survives_restart(tmp_path, monkeypatch):
    from core.analyzers.binary import BinaryAnalyzer

    target = tmp_path / "sample.exe"
    target.write_bytes(b"MZ")
    first = ProjectIntelligenceEngine(str(tmp_path), {"binaries"}).analyze()
    assert str(target) in first.binaries

    def unexpected(*args):
        raise AssertionError("Unchanged binary was analyzed again")

    monkeypatch.setattr(BinaryAnalyzer, "analyze", unexpected)
    second = ProjectIntelligenceEngine(str(tmp_path), {"binaries"}).analyze()
    assert second.binaries == first.binaries
    assert second.structure_nodes == first.structure_nodes


def test_metrics_match_radon_and_reuse_unchanged_files(tmp_path, monkeypatch):
    from radon.metrics import mi_visit
    from radon.complexity import cc_visit
    from radon import raw
    source = tmp_path / "sample.py"
    text = 'class A:\n    def run(self, value):\n        # comment\n        if value:\n            return 1\n        return 0\n'
    source.write_text(text, encoding='utf-8')
    engine = ProjectIntelligenceEngine(str(tmp_path), {'metrics'})
    first = engine.analyze().files[str(source)].metrics
    assert first['maintainability_index'] == round(mi_visit(text, multi=True), 2)
    blocks = cc_visit(text)
    assert first['radon_complexity'] == round(sum(b.complexity for b in blocks) / len(blocks), 2)
    original = raw.analyze
    calls = []
    def count(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(raw, 'analyze', count)
    assert engine.analyze().files[str(source)].metrics == first
    assert ProjectIntelligenceEngine(str(tmp_path), {'metrics'}).analyze().files[str(source)].metrics == first
    assert calls == []
    source.write_text(text + '\n# changed\n', encoding='utf-8')
    engine.analyze()
    assert len(calls) == 1


def test_security_cache_rechecks_edits_and_removes_deleted_findings(tmp_path, monkeypatch):
    from bandit.core.manager import BanditManager
    source = tmp_path / 'unsafe.py'
    other = tmp_path / 'safe.py'
    source.write_text('assert True\n', encoding='utf-8')
    other.write_text('value = 1\n', encoding='utf-8')
    calls = []
    original = BanditManager.discover_files
    def discover(self, files, *args, **kwargs):
        calls.append(list(files))
        return original(self, files, *args, **kwargs)
    monkeypatch.setattr(BanditManager, 'discover_files', discover)
    engine = ProjectIntelligenceEngine(str(tmp_path), {'security'})
    first = engine.analyze()
    second = ProjectIntelligenceEngine(str(tmp_path), {'security'}).analyze()
    assert first.issues == second.issues
    assert any(i.code == 'B101' for i in second.issues)
    assert len(calls) == 1
    source.write_text('value = 2\n', encoding='utf-8')
    assert not any(i.code == 'B101' for i in engine.analyze().issues)
    assert calls[-1] == [str(source)]
    source.unlink()
    engine.analyze()
    assert str(source) not in engine._security_cache
    engine.set_root(str(tmp_path))
    assert not engine._security_cache and not engine._metrics_cache


def test_security_cache_preserves_bandit_filename_order(tmp_path):
    for name in ('a.py', 'Z.py'):
        (tmp_path / name).write_text('assert True\n', encoding='utf-8')
    engine = ProjectIntelligenceEngine(str(tmp_path), {'security'})
    for _ in range(2):
        findings = [i.file for i in engine.analyze().issues if i.code == 'B101']
        assert findings == [str(tmp_path / 'Z.py'), str(tmp_path / 'a.py')]


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

