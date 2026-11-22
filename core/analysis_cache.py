"""Best-effort project storage for file-local analyzer results."""
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys

from core.app_version import current_version
from core.project_state import PROJECT_FOLDER, write_json


# Bump when the cached schema or analysis semantics change within a release.
SCHEMA_VERSION = 1
_PACKAGES = {
    "source": ("tree-sitter", "tree-sitter-language-pack"),
    "binary": ("lief", "capstone"),
    "metrics": ("radon",),
    "security": ("bandit",),
}


def _package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


class AnalyzerCache:
    def __init__(self, root: Path):
        self._directory = root / PROJECT_FOLDER / "analysis-cache"
        self._application = current_version()
        self._versions = {}

    def _entry(self, analyzer, path, digest):
        identity = sha256(str(path).encode("utf-8")).hexdigest()
        if analyzer not in self._versions:
            self._versions[analyzer] = [_package_version(name) for name in _PACKAGES[analyzer]]
        key = [SCHEMA_VERSION, self._application, list(sys.version_info[:3]), self._versions[analyzer], digest]
        return self._directory / analyzer / f"{identity}.json", key

    def load(self, analyzer, path, digest, decode):
        if digest is None:
            return None
        target, key = self._entry(analyzer, path, digest)
        try:
            entry = json.loads(target.read_text(encoding="utf-8"))
            if entry["key"] == key:
                return decode(entry["result"])
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
        return None

    def store(self, analyzer, path, digest, result):
        if digest is None:
            return
        target, key = self._entry(analyzer, path, digest)
        try:
            write_json(target, {"key": key, "result": result})
        except (OSError, ValueError, TypeError, RecursionError):
            pass
