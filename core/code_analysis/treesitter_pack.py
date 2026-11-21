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
    try:
        from tree_sitter_language_pack import available_languages, downloaded_languages

        name = pack_language(language)
        return name in available_languages() or name in downloaded_languages()
    except (ImportError, OSError, RuntimeError):
        return False
