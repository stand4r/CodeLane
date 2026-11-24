"""Generate local Python checks without importing project code or calling an API."""
import os
from pathlib import Path
from uuid import uuid4

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox"}


def generate_project_tests(root):
    root = Path(root).resolve()
    paths = []
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in SKIP
                            and not Path(directory, name).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix == ".py" and not path.is_symlink() and not name.startswith("test_codelane_offline_"):
                paths.append(path.relative_to(root).as_posix())
    if not paths:
        raise ValueError("В проекте нет Python-файлов. Офлайн-генератор пока поддерживает Python.")
    folder = root / "tests"
    if folder.is_symlink():
        raise ValueError("Папка tests является символической ссылкой: выберите обычную папку проекта.")
    folder.mkdir(exist_ok=True)
    target = folder / ("test_codelane_offline_" + uuid4().hex + ".py")
    source = '"""Локальные проверки синтаксиса и примеров doctest. Не проверяют бизнес-требования.\nЗапуск: python -m pytest <этот файл> или python <этот файл>.\nПримеры doctest исполняют код проекта при запуске тестов.\n"""\nimport ast\nimport doctest\nimport importlib\nimport sys\nimport tokenize\nimport unittest\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\nPATHS = __PATHS__\n\nclass OfflineProjectTests(unittest.TestCase):\n    def test_python_syntax(self):\n        for relative in PATHS:\n            with self.subTest(path=relative):\n                with tokenize.open(ROOT / relative) as stream:\n                    compile(stream.read(), relative, "exec", dont_inherit=True)\n\n    def test_documented_examples(self):\n        sys.path.insert(0, str(ROOT))\n        try:\n            found = 0\n            for relative in PATHS:\n                with tokenize.open(ROOT / relative) as stream:\n                    tree = ast.parse(stream.read(), filename=relative)\n                docs = [ast.get_docstring(node) or "" for node in ast.walk(tree)\n                        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]\n                if not any(">>>" in doc for doc in docs):\n                    continue\n                parts = list(Path(relative).with_suffix("").parts)\n                if parts[-1] == "__init__":\n                    parts.pop()\n                if not parts or not all(part.isidentifier() for part in parts):\n                    continue\n                with self.subTest(path=relative):\n                    module = importlib.import_module(".".join(parts))\n                    self.assertEqual(Path(module.__file__).resolve(), (ROOT / relative).resolve())\n                    result = doctest.testmod(module, optionflags=doctest.ELLIPSIS)\n                    found += result.attempted\n                    self.assertEqual(result.failed, 0, relative)\n            if not found:\n                self.skipTest("В проекте нет исполняемых примеров doctest")\n        finally:\n            sys.path.remove(str(ROOT))\n\nif __name__ == "__main__":\n    unittest.main()\n'
    source = source.replace("__PATHS__", repr(paths))
    with target.open("x", encoding="utf-8") as stream:
        stream.write(source)
    return target, len(paths)
