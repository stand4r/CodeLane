"""Round-trip editor text through its original disk format."""

from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from collections import Counter
from dataclasses import dataclass, replace
import re


BYTE_MARKS = (
    (BOM_UTF32_LE, "utf-32-le"), (BOM_UTF32_BE, "utf-32-be"),
    (BOM_UTF16_LE, "utf-16-le"), (BOM_UTF16_BE, "utf-16-be"),
    (BOM_UTF8, "utf-8"),
)
_LINE_END = re.compile(r"\r\n|[\r\n]")


@dataclass(frozen=True)
class TextFileFormat:
    encoding: str = "utf-8"
    bom: bytes = b""
    newline: str = "\n"
    original_lines: tuple[str, ...] = ()
    original_endings: tuple[str, ...] = ()

    def as_utf8(self):
        return replace(self, encoding="utf-8", bom=b"")

    def encode(self, text: str) -> bytes:
        text = _LINE_END.sub("\n", text)
        if self.original_endings:
            text = self._restore_mixed_endings(text)
        elif self.newline != "\n":
            text = text.replace("\n", self.newline)
        return self.bom + text.encode(self.encoding)

    def after_save(self, payload):
        if not self.original_endings:
            return self
        text = payload[len(self.bom):].decode(self.encoding)
        return _format_for_text(text, self.encoding, self.bom)

    def _restore_mixed_endings(self, text):
        lines = text.split("\n")
        old = self.original_lines
        old_endings = self.original_endings
        prefix = 0
        while prefix < min(len(old), len(lines)) and old[prefix] == lines[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(old), len(lines)) - prefix and old[-suffix - 1] == lines[-suffix - 1]:
            suffix += 1
        endings = [self.newline] * (len(lines) - 1)
        # Keep separators where a line survived. In the edited span, retain
        # separators in order; extra lines use the predominant separator.
        before = min(prefix, len(endings))
        endings[:before] = old_endings[:before]
        after = max(before, len(lines) - suffix)
        prior_after = len(old) - suffix
        available = old_endings[before:prior_after]
        middle = min(max(0, min(after, len(endings)) - before), len(available))
        endings[before:before + middle] = available[:middle]
        if after < len(endings):
            endings[after:] = old_endings[prior_after:]
        return "".join(line + ending for line, ending in zip(lines, endings)) + lines[-1]


def decode_text_file(data: bytes) -> tuple[str, TextFileFormat]:
    bom = b""
    for marker, codec in BYTE_MARKS:
        if data.startswith(marker):
            bom, encoding = marker, codec
            text = data[len(marker):].decode(codec)
            break
    else:
        for encoding in ("utf-8", "cp1251", "latin-1"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return text, _format_for_text(text, encoding, bom)


def _format_for_text(text, encoding, bom):
    endings = _LINE_END.findall(text)
    counts = Counter(endings)
    newline = counts.most_common(1)[0][0] if counts else "\n"
    if len(counts) > 1:
        return TextFileFormat(encoding, bom, newline, tuple(_LINE_END.split(text)), tuple(endings))
    return TextFileFormat(encoding, bom, newline)
