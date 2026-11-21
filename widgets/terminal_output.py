"""Reading and folding controls for a terminal's output."""
from contextlib import contextmanager

from PyQt6 import QtCore, QtGui, QtWidgets
from design.icons import set_line_icon


_ROUND_BUTTON_STYLE = (
    "QToolButton { border-radius: %dpx; border: 0; padding: 0; min-height: 0; "
    "background: palette(mid); color: palette(text); }"
    "QToolButton:hover, QToolButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
)


class TerminalOutputControls(QtWidgets.QWidget):
    def __init__(self, screen, parent=None):
        super().__init__(parent)
        self.screen = screen
        self.unread = 0
        self.errors = 0
        self.groups = []
        self.last_block = None
        self.next_block = 0
        self.updating = False
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        self.resume = QtWidgets.QToolButton(self)
        self.resume.setAccessibleName("К новым строкам")
        self.resume.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.resume.clicked.connect(self.follow)
        layout.addWidget(self.resume, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.expand = QtWidgets.QToolButton(screen.viewport())
        self.expand.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.expand.setAccessibleName("Показать или скрыть повторяющиеся строки")
        self.expand.setCheckable(True)
        self.expand.toggled.connect(self.set_expanded)
        self.resume.hide()
        self.expand.hide()
        screen.verticalScrollBar().valueChanged.connect(self.scrolled)
        screen.viewport().installEventFilter(self)
        self.on_ui_scale_changed()

    def on_ui_scale_changed(self, scale=None):
        from app.ui_scale import scale_px

        diameter = scale_px(30)
        for button in (self.resume, self.expand):
            button.setFixedSize(diameter, diameter)
            button.setStyleSheet(_ROUND_BUTTON_STYLE % (diameter // 2))
        set_line_icon(self.resume, "chevron-down", scale_px(16))
        self._position_expand()

    def _position_expand(self):
        from app.ui_scale import scale_px

        inset = scale_px(8)
        viewport = self.screen.viewport()
        self.expand.move(max(0, viewport.width() - self.expand.width() - inset), inset)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Resize:
            self._position_expand()
        return super().eventFilter(watched, event)

    def scrolled(self):
        bar = self.screen.verticalScrollBar()
        if not self.updating and bar.value() >= bar.maximum() - 2:
            self.unread = self.errors = 0
            self.resume.hide()

    def follow(self):
        bar = self.screen.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.scrolled()

    @contextmanager
    def append(self, text="", error=False):
        bar = self.screen.verticalScrollBar()
        position = bar.value()
        paused = position < bar.maximum() - 2
        selection = QtGui.QTextCursor(self.screen.textCursor())
        self.updating = True
        try:
            yield
        finally:
            if paused:
                self.screen.setTextCursor(selection)
                bar.setValue(position)
                self.unread += text.count("\n")
                self.errors += int(error)
                self.resume.setToolTip(f"К новым строкам ({self.unread})")
                self.resume.setVisible(bool(self.unread))
            self.updating = False

    def fold_output(self, start_position):
        document = self.screen.document()
        before = len(self.groups)
        block = document.findBlock(max(start_position, self.next_block))
        while block.isValid() and block.next().isValid():
            previous = self.last_block
            if (block.text() and previous is not None and previous.isValid()
                    and previous.next() == block and previous.text() == block.text()):
                self.groups.append(block)
                if not self.expand.isChecked():
                    block.setVisible(False)
                    block.setLineCount(0)
            self.last_block = block
            self.next_block = block.next().position()
            block = block.next()
        if len(self.groups) != before:
            count = len(self.groups)
            self.expand.setText(str(count) if count < 100 else "99+")
            self.expand.setToolTip(f"Показать или скрыть повторы ({count})")
            self._position_expand()
            self.expand.raise_()
            self.expand.show()
            document.markContentsDirty(start_position, document.characterCount() - start_position)
            self.screen.viewport().update()

    def set_expanded(self, expanded):
        for block in self.groups:
            if block.isValid():
                block.setVisible(expanded)
                block.setLineCount(1 if expanded else 0)
        document = self.screen.document()
        document.markContentsDirty(0, document.characterCount())
        self.screen.viewport().update()

    def reset(self):
        self.groups.clear()
        self.last_block = None
        self.next_block = 0
        self.unread = self.errors = 0
        self.resume.hide()
        self.expand.hide()
        self.expand.setChecked(False)
