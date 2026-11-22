"""Rich rendering of assistant answers in the AI chat.

Two things differ from a plain rich-text label:

* inline code is drawn as a **rounded chip**. Qt rich text cannot round an
  inline background, so the text uses a sentinel background colour that is
  converted into a custom character property and painted as a rounded
  rectangle behind the glyphs.
* fenced code blocks are rendered with the **real editor core**
  (``ModernSyntaxHighlighter`` from ``widgets.QCodeEditor``) so answers are
  highlighted exactly like the code editor.
"""
from __future__ import annotations

import html
import re
from typing import List, Optional, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

__all__ = ["AssistantMessage", "CodeSnippet", "InlineCodeText",
           "split_markdown_blocks", "markdown_to_html"]

# Rich text cannot carry a radius, so inline code is marked with a colour that
# is re-interpreted as a custom property after parsing.
SENTINEL_BG = "#010203"
INLINE_PROP = int(QtGui.QTextFormat.Property.UserProperty) + 1

CHIP_RADIUS = 5.0
CHIP_PAD_X = 2.0


def _forward_wheel_to_chat(widget: QtWidgets.QWidget, event) -> bool:
    """Scroll the surrounding transcript for a wheel event over a text block.

    Paragraphs and short snippets are fitted to their content, so their own
    (hidden) scrollbars must not swallow the wheel — otherwise every block
    behaves like a separate scroller.
    """
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QtWidgets.QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        return False
    bar = parent.verticalScrollBar()
    bar.setValue(bar.value() - event.angleDelta().y())
    return True

LINE_HEIGHT = "145%"


def chat_editor_font() -> QtGui.QFont:
    from app.ui_scale import scale_px
    from utils.settings_manager import get_settings_manager

    try:
        settings = get_settings_manager()
        family = str(settings.get_setting("editor_font", "Cascadia Code"))
        points = int(settings.get_setting("editor_font_size", 13) or 13)
    except RuntimeError:
        family, points = "Cascadia Code", 13
    font = QtGui.QFont(family, scale_px(points))
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    return font


def chat_font_px() -> int:
    return chat_body_font().pixelSize()


def chat_body_font() -> QtGui.QFont:
    from app.ui_scale import scale_px
    from utils.settings_manager import get_settings_manager

    try:
        size = int(get_settings_manager().get_setting("ui_font_size", 13))
    except (RuntimeError, TypeError, ValueError):
        size = 13
    font = QtGui.QFont(QtWidgets.QApplication.font())
    font.setPixelSize(scale_px(max(12, min(size, 15)) + 1))
    return font


def chat_code_font_px() -> int:
    return max(11, chat_font_px() - 1)


def chat_small_px() -> int:
    return max(11, chat_font_px() - 2)


def _tinted_surface(palette, amount):
    base = palette.color(QtGui.QPalette.ColorRole.Window)
    text = palette.color(QtGui.QPalette.ColorRole.WindowText)
    return QtGui.QColor(*[
        round(start + (end - start) * amount)
        for start, end in zip(base.getRgb()[:3], text.getRgb()[:3])
    ])


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------
def split_markdown_blocks(text: str) -> List[Tuple[str, object]]:
    """Split an answer into consecutive ``("text", html)`` and ``("code", …)``.

    Code fences become separate widgets, so the text parts are rendered without
    any ``<pre>`` markup.
    """
    lines = str(text or "").splitlines()
    blocks: List[Tuple[str, object]] = []
    text_lines: List[str] = []
    code_lines: List[str] = []
    code_lang = ""
    fence = None

    def flush_text():
        if text_lines:
            html_text = markdown_to_html("\n".join(text_lines))
            if html_text.strip():
                blocks.append(("text", html_text))
            text_lines.clear()

    for line in lines:
        match = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence is not None:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                blocks.append(("code", (code_lang or "text",
                                         "\n".join(code_lines))))
                code_lines.clear()
                fence = None
            else:
                code_lines.append(line)
        elif match:
            flush_text()
            fence = match[1]
            info = match[2].split()
            code_lang = info[0] if info else ""
        else:
            text_lines.append(line)
    if fence is not None:
        blocks.append(("code", (code_lang or "text", "\n".join(code_lines))))
    flush_text()
    return blocks


def _inline(value: str, chip: bool = True) -> str:
    code_fragments = []

    def save_code(match):
        background = f"background:{SENTINEL_BG}; " if chip else ""
        code = match[1]
        if chip:
            code = f"&#8201;{code}&#8201;"
        code_fragments.append(f'<code style="{background}font-size:{chat_code_font_px()}px;">{code}</code>')
        return f"\0{len(code_fragments) - 1}\0"

    safe = html.escape(value.replace("\0", "\uFFFD"))
    safe = re.sub(r"`([^`\n]+)`", save_code, safe)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", safe)
    safe = re.sub(r"\[([^\]]+)\]\((https?://[^)\s<>\0]+)\)",
                  r'<a href="\2"><u>\1</u></a>', safe)
    return re.sub(r"\0(\d+)\0", lambda marker: code_fragments[int(marker[1])], safe)


_LIST_ITEM = re.compile(r"^( *)([-*+]|(\d+)[.)])\s+(.*)$")


def _render_list(lines: List[str], index: int, first: re.Match) -> tuple[str, int]:
    indent = len(first[1])
    tag = "ol" if first[3] else "ul"
    start = f' start="{first[3]}"' if first[3] else ""
    items = []
    while index < len(lines):
        item = _LIST_ITEM.match(lines[index])
        if not item or len(item[1]) != indent or bool(item[3]) != bool(first[3]):
            break
        content = re.sub(r"^\[([ xX])\]\s+", lambda mark: "☐ " if mark[1] == " " else "☑ ", item[4])
        item_html = _inline(content, chip=False)
        index += 1
        while index < len(lines) and lines[index].startswith(" " * (indent + 2)) and lines[index].strip():
            nested = _LIST_ITEM.match(lines[index])
            if nested:
                child_html, index = _render_list(lines, index, nested)
                item_html += child_html
            else:
                item_html += " " + _inline(lines[index].strip(), chip=False)
                index += 1
        items.append(f"<li>{item_html}</li>")
    return f'<{tag}{start} style="margin:8px 0 12px 0;">{"".join(items)}</{tag}>', index


def markdown_to_html(text: str) -> str:
    """Convert prose markdown into safe rich text (no code fences here).

    Consecutive text lines form a paragraph, while lists and headings remain
    separate blocks.
    """
    lines = str(text or "").splitlines()
    blocks: List[str] = []
    paragraph: List[str] = []
    index = 0
    body = chat_font_px()
    palette = QtWidgets.QApplication.palette()
    table_header = _tinted_surface(palette, 0.09).name()
    border = _tinted_surface(palette, 0.23).name()
    quote_text = _tinted_surface(palette, 0.78).name()

    def flush():
        if paragraph:
            text = " ".join(paragraph)
            # A line that is entirely bold is the model's sub-heading.
            if len(paragraph) == 1 and re.fullmatch(r"<b>.+</b>", text, flags=re.S):
                blocks.append(
                    f'<p style="margin:14px 0 8px 0; font-size:{body + 2}px;'
                    f' font-weight:600; line-height:1.35;">{text}</p>')
            else:
                blocks.append(
                    f'<p style="margin:0 0 8px 0; font-size:{body}px;'
                    f' line-height:{LINE_HEIGHT};">' + text + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        # Table
        if (stripped.startswith("|") and index + 1 < len(lines)
                and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[index + 1])
                and "-" in lines[index + 1]):
            flush()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([c.strip() for c in
                             lines[index].strip().strip("|").split("|")])
                index += 1
            column_width = 100 / len(header)
            head_html = "".join(
                f'<th align="left" valign="top" width="{column_width:.2f}%"'
                f' style="padding:9px 12px;'
                f' font-size:{body - 2}px; font-weight:600;'
                f' background:{table_header};'
                f' border-bottom:1px solid {border};">{_inline(c, chip=False)}</th>'
                for c in header)
            body_html = ""
            for cells in rows:
                padded = (cells + [""] * len(header))[:len(header)]
                body_html += "<tr>" + "".join(
                    f'<td valign="top" style="padding:10px 12px;'
                    f' font-size:{body}px; line-height:{LINE_HEIGHT};'
                    f' border-bottom:1px solid {border};">'
                    f'{_inline(c, chip=False)}</td>' for c in padded) + "</tr>"
            blocks.append(
                f'<table cellspacing="0" cellpadding="0" width="100%"'
                f' style="margin:12px 0 16px 0; border-collapse:collapse;">'
                f'<tr>{head_html}</tr>{body_html}</table>')
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            size = body + {1: 6, 2: 3, 3: 2}.get(level, 1)
            top = 18 if level <= 2 else 14
            blocks.append(
                f'<p style="margin:{top}px 0 8px 0; font-size:{size}px;'
                f' font-weight:600; line-height:130%;">{_inline(heading.group(2))}</p>')
            index += 1
            continue

        list_item = _LIST_ITEM.match(line)
        if list_item:
            flush()
            list_html, index = _render_list(lines, index, list_item)
            blocks.append(list_html)
            continue

        if stripped.startswith(">"):
            flush()
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(_inline(lines[index].strip().lstrip(">").strip()))
                index += 1
            blocks.append(
                f'<table cellspacing="0" cellpadding="0" style="margin:10px 0;"><tr>'
                f'<td width="3" bgcolor="{border}"></td>'
                f'<td style="padding:6px 12px; color:{quote_text};'
                f' font-size:{body}px; line-height:{LINE_HEIGHT};">'
                f'{"<br>".join(quote)}</td></tr></table>')
            continue

        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            flush()
            blocks.append(f'<hr style="margin:14px 0;" color="{border}">')
            index += 1
            continue

        standalone_heading = re.fullmatch(r"\*\*.+?\*\*", stripped)
        if standalone_heading:
            flush()
        paragraph.append(_inline(stripped))
        if standalone_heading or line.endswith("  "):
            flush()
        index += 1

    flush()
    return "".join(blocks)


# ---------------------------------------------------------------------------
# Inline code text
# ---------------------------------------------------------------------------
class InlineCodeText(QtWidgets.QTextBrowser):
    """Read-only rich text with rounded chips behind inline code.

    The text height always follows the content, so the widget can live inside a
    normal vertical layout instead of scrolling on its own.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatRichText")
        self.setReadOnly(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.document().setDocumentMargin(0)
        # Long URLs / paths without spaces used to overflow the line and get
        # clipped at the bubble edge; break anywhere as a last resort.
        option = self.document().defaultTextOption()
        option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse | QtCore.Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setOpenExternalLinks(True)
        self._chips: List[Tuple[int, int, int]] = []
        self.viewport().installEventFilter(self)

    # -- scrolling -----------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.viewport() and event.type() == QtCore.QEvent.Type.Wheel:
            if _forward_wheel_to_chat(self, event):
                return True
        return super().eventFilter(obj, event)

    # -- content -------------------------------------------------------
    def set_html(self, html_text: str):
        self.document().setDefaultFont(chat_body_font())
        palette = QtWidgets.QApplication.palette()
        text = palette.color(QtGui.QPalette.ColorRole.Text).name()
        link = "#8EC3FF" if palette.color(QtGui.QPalette.ColorRole.Window).lightness() < 128 else "#19558A"
        self.document().setDefaultStyleSheet(f"a {{ color: {link}; }}")
        body = (f'<div style="color:{text}; font-size:{chat_font_px()}px;'
                f' line-height:{LINE_HEIGHT};">{html_text or ""}</div>')
        self.setHtml(body)
        self._normalize_tables()
        self._decorate_inline_code()
        self._fit_height()

    def _normalize_tables(self):
        """Make markdown tables fit the bubble instead of overflowing it.

        Qt sizes table columns from their content, so a three-column table with
        paths and Russian headings was drawn wider than the 300px sidebar and
        the right column was clipped. A percentage width plus small padding
        keeps the table inside the text column.
        """
        document = self.document()
        stack = [document.rootFrame()]
        while stack:
            frame = stack.pop()
            if isinstance(frame, QtGui.QTextTable):
                # List rows are 2-column tables with a short marker cell; they
                # must keep their own column widths instead of being stretched.
                marker = ""
                if frame.columns() == 2 and frame.rows() > 0:
                    marker = frame.cellAt(0, 0).firstCursorPosition().block().text().strip()
                is_list_row = bool(re.fullmatch(r"(•|\d{1,3}\.)", marker))
                if not is_list_row:
                    fmt = frame.format()
                    fmt.setWidth(QtGui.QTextLength(QtGui.QTextLength.Type.PercentageLength, 100))
                    fmt.setCellPadding(0)
                    fmt.setCellSpacing(0)
                    frame.setFormat(fmt)
            stack.extend(frame.childFrames())

    def wheelEvent(self, event):
        # The transcript is one scroll area; these blocks are fitted to their
        # content, so the wheel must scroll the chat, not the paragraph.
        event.ignore()

    def _decorate_inline_code(self):
        """Turn the sentinel background into a custom property (chip marker)."""
        document = self.document()
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    if fmt.background().style() != QtCore.Qt.BrushStyle.NoBrush and \
                            fmt.background().color().name().lower() == SENTINEL_BG:
                        cursor = QtGui.QTextCursor(document)
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(),
                                           QtGui.QTextCursor.MoveMode.KeepAnchor)
                        new_fmt = QtGui.QTextCharFormat(fmt)
                        new_fmt.clearBackground()
                        new_fmt.setProperty(INLINE_PROP, True)
                        cursor.setCharFormat(new_fmt)
                        iterator = block.begin()
                        continue
                iterator += 1
            block = block.next()

    # -- geometry ------------------------------------------------------
    def _fit_height(self):
        height = self.document().size().height()
        self.setFixedHeight(max(18, int(height) + 6))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    # -- painting ------------------------------------------------------
    def paintEvent(self, event):
        self._paint_chips()
        super().paintEvent(event)

    def _paint_chips(self):
        document = self.document()
        layout = document.documentLayout()
        if layout is None:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(_tinted_surface(QtWidgets.QApplication.palette(), 0.14))
        try:
            block = document.begin()
            while block.isValid():
                text_layout = block.layout()
                block_rect = layout.blockBoundingRect(block)
                if text_layout is not None:
                    iterator = block.begin()
                    while not iterator.atEnd():
                        fragment = iterator.fragment()
                        if fragment.isValid() and                                 fragment.charFormat().property(INLINE_PROP):
                            self._paint_fragment_chip(
                                painter, block, text_layout, block_rect, fragment)
                        iterator += 1
                block = block.next()
        finally:
            painter.end()

    @staticmethod
    def _paint_fragment_chip(painter, block, text_layout, block_rect, fragment):
        """Draw the chip behind one inline-code fragment (possibly wrapped)."""
        start = fragment.position() - block.position()
        end = start + fragment.length()
        if start < 0 or text_layout.lineCount() == 0:
            return
        line = text_layout.lineForTextPosition(start)
        if not line.isValid():
            return
        guard = 0
        while line.isValid() and guard < 64:
            guard += 1
            line_start = line.textStart()
            line_end = line_start + line.textLength()
            if line_start >= end:
                break
            seg_start = max(start, line_start)
            seg_end = min(end, line_end)
            if seg_end <= seg_start:
                break
            x1 = line.cursorToX(seg_start)[0]
            x2 = line.cursorToX(seg_end)[0]
            rect = QtCore.QRectF(
                min(x1, x2) - CHIP_PAD_X,
                block_rect.top() + line.y() + 1,
                abs(x2 - x1) + CHIP_PAD_X * 2,
                line.height() - 2)
            painter.drawRoundedRect(rect, CHIP_RADIUS, CHIP_RADIUS)
            next_index = line.lineNumber() + 1
            if next_index >= text_layout.lineCount():
                break
            line = text_layout.lineAt(next_index)


# ---------------------------------------------------------------------------
# Code block on top of the editor core
# ---------------------------------------------------------------------------
class _CodeView(QtWidgets.QPlainTextEdit):
    """Read-only code view that hands the wheel to the chat when it can't scroll.

    Without this, the wheel over a short snippet scrolled the snippet instead
    of the conversation — every block felt like its own scroll area.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().installEventFilter(self)

    def _can_scroll(self) -> bool:
        return (self.verticalScrollBar().maximum() > 0
                or self.horizontalScrollBar().maximum() > 0)

    def eventFilter(self, obj, event):
        if obj is self.viewport() and event.type() == QtCore.QEvent.Type.Wheel:
            if not self._can_scroll() and _forward_wheel_to_chat(self, event):
                return True
        return super().eventFilter(obj, event)


class CodeSnippet(QtWidgets.QWidget):
    """One fenced code block, highlighted by the editor's own highlighter."""

    MAX_HEIGHT = 380

    def __init__(self, code: str, language: str = "", parent=None):
        super().__init__(parent)
        self._code = code.rstrip("\n")
        self._language = (language or "text").strip().lower()
        self.setObjectName("chatCodeSnippet")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("chatCodeHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(10, 3, 6, 3)
        header_layout.setSpacing(6)
        title = QtWidgets.QLabel(self._language)
        title.setObjectName("chatCodeLanguage")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        self.copy_btn = QtWidgets.QToolButton()
        self.copy_btn.setObjectName("chatCodeCopy")
        self.copy_btn.setText("Копировать")
        self.copy_btn.setAccessibleName("Скопировать блок кода")
        self.copy_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        self.copy_btn.clicked.connect(self.copy_code)
        header_layout.addWidget(self.copy_btn)
        root.addWidget(header)

        self.view = _CodeView()
        self.view.setObjectName("chatCodeView")
        self.view.setReadOnly(True)
        self.view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.view.setPlainText(self._code)
        self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Wrapping keeps narrow chat columns readable; a horizontal scrollbar
        # inside a 300px bubble was hiding half of every long code line.
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.document().setDocumentMargin(10)
        self._apply_font()
        self._install_highlighter()
        root.addWidget(self.view)
        self._fit_height()

    def _apply_font(self):
        self.view.setFont(chat_editor_font())

    def _install_highlighter(self):
        """Highlight with the code editor's own highlighter and rules."""
        try:
            from core.language_services import LanguageProviderFactory
            from widgets.QCodeEditor import ModernSyntaxHighlighter
        except Exception:
            return
        language = self._language
        aliases = {"py": "python", "js": "javascript", "ts": "typescript",
                   "bash": "shell", "sh": "shell", "text": "python"}
        language = aliases.get(language, language)
        try:
            config = LanguageProviderFactory.get_language_config(language)
        except Exception:
            return
        try:
            background = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Base)
            self.highlighter = ModernSyntaxHighlighter(
                self.view.document(), config, light_background=background.lightness() >= 128,
            )
        except Exception:
            self.highlighter = None

    def _fit_height(self):
        """Height from the *wrapped* line count, not the document size.

        ``document().size()`` is meaningless before the widget has a width, so
        the old calculation clipped multi-line snippets to a single line. Long
        lines are wrapped (there is no horizontal scrollbar any more), so their
        visual line count is estimated from the font metrics.
        """
        metrics = QtGui.QFontMetrics(self.view.font())
        viewport_width = self.view.viewport().width()
        if viewport_width <= 0:
            viewport_width = max(1, self.width() - 2)
        available = max(40, viewport_width - 20)   # document margin + padding
        wrapped_lines = 0
        for line in (self._code.splitlines() or [""]):
            advance = metrics.horizontalAdvance(line)
            wrapped_lines += max(1, -(-advance // available))
        content = wrapped_lines * metrics.lineSpacing() + 18
        if content > self.MAX_HEIGHT:
            self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.view.setFixedHeight(self.MAX_HEIGHT)
        else:
            self.view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.view.setFixedHeight(max(28, int(content)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def copy_code(self):
        try:
            QtWidgets.QApplication.clipboard().setText(self._code)
            self.copy_btn.setText("Скопировано")
            QtCore.QTimer.singleShot(
                1200, lambda: self.copy_btn.setText("Копировать")
                if self.copy_btn else None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Whole message
# ---------------------------------------------------------------------------
class AssistantMessage(QtWidgets.QWidget):
    """An assistant answer: prose blocks interleaved with code snippets."""

    def __init__(self, markdown: str = "", parent=None):
        super().__init__(parent)
        self._width = 520
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self._parts: List[QtWidgets.QWidget] = []
        self._markdown = ""
        self.set_markdown(markdown or "")

    # -- content -------------------------------------------------------
    def set_markdown(self, markdown: str):
        self._markdown = markdown or ""
        self._appearance = (chat_body_font(), chat_editor_font(), QtWidgets.QApplication.palette().cacheKey())
        for part in self._parts:
            try:
                self._layout.removeWidget(part)
                part.setParent(None)
                part.deleteLater()
            except RuntimeError:
                pass
        self._parts.clear()
        for kind, payload in split_markdown_blocks(self._markdown):
            if kind == "code":
                language, code = payload
                widget = CodeSnippet(code, language, self)
            else:
                widget = InlineCodeText(self)
                widget.set_html(payload)
            self._layout.addWidget(widget)
            self._parts.append(widget)
        self.set_width(self._width)

    def on_ui_scale_changed(self, scale):
        if self._appearance != (chat_body_font(), chat_editor_font(), QtWidgets.QApplication.palette().cacheKey()):
            self.set_markdown(self._markdown)

    def text(self) -> str:
        return self._markdown

    def plain_text(self) -> str:
        return self._markdown

    # -- geometry ------------------------------------------------------
    def set_width(self, width: int):
        self._width = max(180, int(width))
        for part in self._parts:
            try:
                part.setFixedWidth(self._width)
                if isinstance(part, InlineCodeText):
                    part._fit_height()
                elif isinstance(part, CodeSnippet):
                    part._fit_height()
            except RuntimeError:
                pass
        self.updateGeometry()
