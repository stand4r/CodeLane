"""Commit summary helpers: changed sources, analyzer warnings, diffstat text."""
import pytest

pytest.importorskip("PyQt6")

from PyQt6 import QtWidgets  # noqa: E402

from widgets.GitPanel import (  # noqa: E402
    GitPanel,
    changed_source_paths,
    count_analyzer_warnings,
)

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


STATUS = ("## main\n"
          " M core/a.py\n"
          "?? notes.txt\n"
          "R  old_name.py -> new_name.py\n"
          "A  assets/logo.png\n")


def test_changed_source_paths_filters_binaries_and_renames(tmp_path):
    for name in ("core/a.py", "new_name.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/logo.png").write_bytes(b"\x89PNG")

    paths = changed_source_paths(str(tmp_path), STATUS)
    names = sorted(p.replace("\\", "/").split("/")[-1] for p in paths)
    assert names == ["a.py", "new_name.py"]
    assert all(p.startswith(str(tmp_path)) for p in paths)


def test_count_analyzer_warnings_is_non_negative_and_cached(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    cache = {}
    first = count_analyzer_warnings(str(tmp_path), " M demo.py\n", cache)
    assert isinstance(first, int) and first >= 0
    assert str(source) in cache
    mtime, issues = cache[str(source)]
    assert issues == first
    assert count_analyzer_warnings(str(tmp_path), " M demo.py\n", cache) == first
    source.write_text("def f(:\n    return 1\n", encoding="utf-8")
    refreshed = count_analyzer_warnings(str(tmp_path), " M demo.py\n", cache)
    assert isinstance(refreshed, int) and refreshed >= 0


def test_count_analyzer_warnings_without_root_is_zero():
    assert count_analyzer_warnings("", " M demo.py\n") == 0


def test_files_word_pluralizes():
    assert GitPanel._files_word(1) == "файл"
    assert GitPanel._files_word(3) == "файла"
    assert GitPanel._files_word(11) == "файлов"
