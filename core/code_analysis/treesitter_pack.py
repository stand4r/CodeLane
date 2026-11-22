"""Expose the language pack's syntax nodes to the existing analyzers."""

from functools import lru_cache

class PackSyntaxNode:
    def __init__(self, node):
        self._node = node

    @property
    def type(self):
        return self._node.kind()

    @property
    def start_point(self):
        point = self._node.start_position()
        return point.row, point.column

    @property
    def end_point(self):
        point = self._node.end_position()
        return point.row, point.column

    @property
    def start_byte(self):
        return self._node.start_byte()

    @property
    def end_byte(self):
        return self._node.end_byte()

    @property
    def is_error(self):
        return self._node.is_error()

    @property
    def has_error(self):
        return self._node.has_error()

    @property
    def children(self):
        return [PackSyntaxNode(self._node.child(i)) for i in range(self._node.child_count())]

    def child_by_field_name(self, name):
        node = self._node.child_by_field_name(name)
        return PackSyntaxNode(node) if node is not None else None


def parse_pack_root(parser, source: str):
    return PackSyntaxNode(parser.parse(source).root_node())


def pack_language(language: str) -> str:
    return {"jsx": "tsx", "c_sharp": "csharp", "cs": "csharp"}.get(language, language)


@lru_cache(maxsize=64)
def pack_has_language(language: str) -> bool:
    """Report whether the language pack can provide a grammar for ``language``.

    Important: ``available_languages()`` / ``downloaded_languages()`` only reflect
    the local pack cache (grammars are fetched lazily, so a fresh environment
    reports almost nothing).  The authoritative, cache-independent check is
    ``has_language()``; the cache lists are kept only as a legacy fallback.
    """
    try:
        from tree_sitter_language_pack import (
            available_languages,
            downloaded_languages,
            has_language,
        )
    except (ImportError, OSError, RuntimeError):
        return False

    name = pack_language(language)
    try:
        if has_language(name) or has_language(language):
            return True
    except (OSError, RuntimeError):
        pass

    try:
        # Older pack builds have no has_language(); accept either naming form.
        candidates = {name, language}
        return bool(candidates & set(available_languages())) or bool(
            candidates & set(downloaded_languages())
        )
    except (ImportError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=64)
def pack_probe_language(language: str) -> bool:
    """Verify that a usable parser can really be obtained (downloads if needed).

    Unlike :func:`pack_has_language` this performs the lazy grammar fetch, so it
    answers "is the backend physically usable right now?" (offline hosts return
    ``False``).  Results are cached for the process lifetime.
    """
    try:
        from tree_sitter_language_pack import get_parser

        get_parser(pack_language(language))
        return True
    except ImportError:
        return False
    except Exception:  # noqa: BLE001 - pack raises its own error hierarchy
        return False


def pack_backend_available(language: str = "python") -> bool:
    """True when Level 2 can parse ``language`` through the language pack."""
    return pack_has_language(language)
